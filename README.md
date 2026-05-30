# Argus Alpha Research

Depth-feed-only alpha research and paper trading system for NSE equity futures. All signals are derived exclusively from the 20-level order book (depth feed).

---

## Universe

| Instrument | Lot Size | Notes |
|---|---|---|
| HDFCBANK | 550 | Primary development instrument |
| ICICIBANK | 700 | |
| RELIANCE | 500 | |
| TCS | 175 | Weaker A6 signal — size down |
| BAJFINANCE | — | **Excluded** — data quality too poor |

---

## Data

Raw tick data is stored under `data/raw/depth/` (gitignored — never committed).

Directory structure mirrors the VPS:
```
data/raw/depth/
  trading_date=YYYY-MM-DD/
    symbol=HDFCBANK-May2026-FUT/
      compacted-depth-YYYY-MM-DD-HDFCBANK-*.parquet
```

**Column names (verified from parquet):**

| Field | Columns |
|---|---|
| Timestamp | `collector_received_at` (UTC, converted to IST tz-naive on load) |
| Prices | `bid_price_01`..`bid_price_20`, `ask_price_01`..`ask_price_20` |
| Quantities | `bid_qty_01`..`bid_qty_20`, `ask_qty_01`..`ask_qty_20` |
| Orders | `bid_orders_01`..`bid_orders_20`, `ask_orders_01`..`ask_orders_20` |

Packet rate: ~2–5 packets/sec in compacted parquet.  
Session filter: **9:20–15:25 IST** (avoids open auction and close).

### Syncing data

```bash
./sync_market_data.sh   # sync only market feed from VPS
./sync_data.sh          # sync full depth feed
```

### Data quality

Healthy files have **88,000–101,000 session rows** with a full 365-minute span and zero NaNs. Known bad files:

| Instrument | Date | Issue |
|---|---|---|
| RELIANCE | 2026-05-20 | 12 session rows — corrupt/incomplete |
| TCS | 2026-05-20 | File missing from sync |

`load_depth` automatically rejects files below `MIN_SESSION_ROWS = 50,000` with a `ValueError`. Use `safe_load_depth` in loops to return `None` instead of raising.

---

## Project Structure

```
research/
  data/
    load_data.py              # data loading, derived columns, data quality guard
  features/
    depth_features.py         # A1–A6 feature library + microprice deviation
  backtester/
    maker_engine.py           # maker strategy backtester and cost model
    maker_walkforward.py      # walk-forward runner, grid search, trade log export
  notebooks/
    explore.ipynb             # single-day EDA (HDFCBANK 2026-05-06)
    ic_analysis.ipynb         # IC/ICIR analysis across all instruments and dates
    signal_combination.ipynb  # composite signal construction
paper_trader/
  config.py                   # all strategy and runtime parameters
  signal.py                   # microprice deviation computation
  broker.py                   # per-instrument stateful order simulator
  logger.py                   # append-only CSV event logger
  report.py                   # daily P&L report emailer
  main.py                     # asyncio entry point
  feed_client.py              # Dhan depth + market websocket clients
  dhan_parser.py              # Dhan binary packet parser (depth + market)
  contracts.py                # front-month contract resolver (instrument master CSV)
  logs/                       # paper_trades.csv, paper_orders.csv, paper_pnl.csv
  systemd/                    # 7 systemd service and timer unit files
  requirements.txt
  DEPLOY.md
tests/
  test_backtester.py          # 28 tests for backtesting engine
  test_features.py            # smoke tests for feature library
  test_maker_engine.py        # tests for maker strategy engine
  test_paper_trader.py        # 37 tests for paper trader modules
run_oos_validation.py         # out-of-sample validation runner
```

---

## Signal: Microprice Deviation

The primary trading signal is **microprice deviation** from the L1 order book:

```
microprice       = (bid_price × ask_qty + ask_price × bid_qty) / (bid_qty + ask_qty)
micro_deviation  = microprice − mid
```

A large positive deviation indicates bid-side pressure (bullish); large negative indicates ask-side pressure (bearish). ICIR of **6.9** at h=1 packet in in-sample analysis.

**Entry threshold:** `|micro_deviation| ≥ 0.20`  
**Signal column:** `micro_deviation` (computed in `paper_trader/signal.py` and `research/features/depth_features.py`)

---

## Maker Strategy

Passive limit order strategy that earns the spread rather than paying it.

**Entry:** On a fresh threshold cross, post a limit order at the current L1 bid (BUY) or ask (SELL). "Fresh cross" requires `prev_abs_sig < threshold` so the strategy does not re-enter on a sustained signal.

**Fill detection (depth-only):**
- BUY fills when `bid_price_01 < posted_price` (aggressive sellers consumed our level). SELL fills when `ask_price_01 > posted_price`.
- Matches the backtester's fill model exactly. No market feed connection is needed.

**Queue position approximation:** `queue_ahead = bid_qty_01` at post time; `qty_consumed` = cumulative drop in L1 bid qty since post. Logged with every fill for post-hoc analysis.

**Exit (maker):** Post a passive limit at the current ask (long) or bid (short). Exit fires when the book moves through the posted exit price.

**Exit (taker fallback):** If the position is held for `MAX_HOLD_PACKETS = 500` packets (~200 seconds) without a passive exit, exit at mid ± 1 tick.

**Cooldown:** `ORDER_TIMEOUT_PKTS = 10` packets between a cancel/exit and the next entry.

---

## Fee Model (NSE Equity Futures, Dhan)

| Cost | Rate |
|---|---|
| Brokerage | ₹20 flat per order (×2 per round trip) |
| STT | 0.0125% — sell side only |
| Exchange charge | 0.002% — both sides |
| SEBI fee | 0.0001% — both sides |
| Stamp duty | 0.002% — buy side only |
| GST | 18% on brokerage + exchange + SEBI |

On 1 lot HDFCBANK (~₹9L notional), total round-trip cost ≈ ₹208. STT is applied to the sell-side notional, so fees are slightly asymmetric between long and short trades (~₹0.28 difference at current prices).

---

## Out-of-Sample Validation

Run: `python run_oos_validation.py`

| Period | Dates | Sessions |
|---|---|---|
| Train | 2026-04-24 – 2026-05-15 | 15 days |
| Test (OOS) | 2026-05-18 – 2026-05-22 | 5 days |

**Parameters used:** `entry_threshold=0.20`, `max_hold=500`, `order_timeout=10`, `exit_mode='maker'`, `fresh_cross=True`

> **Live recalibration (2026-05-30):** Entry threshold raised to 0.35, queue fill filter added (qty_consumed ≥ 10% of queue_ahead), and minimum 10-packet hold before exit posting. These correct for live tick data being ~10× noisier than compacted backtester data.

Produces `trade_log_train.csv` and `trade_log_oos.csv` with 18 columns per trade (entry/exit timestamps, prices, methods, lot size, notional, hold duration, gross P&L, fee, net P&L).

> **Caveat:** OOS Sharpe of 11–18 is almost certainly optimistic. Backtest fill model assumes no queue position and no adverse selection. Statistically meaningless with 5 daily observations (SE ≈ ±4–5 Sharpe units). Realistic live Sharpe estimate: 1–5. The paper trading phase exists to measure actual fill rate and net P&L before committing capital.

---

## Walk-Forward Runner

`research/backtester/maker_walkforward.py` provides:

- **`run_maker_walkforward(sessions, params, lot_sizes)`** — streams one session at a time (memory-safe), returns `MakerWalkForwardResult` with per-day P&L, Sharpe, and a full `trade_log` DataFrame.
- **`maker_grid_search(sessions, param_grid, lot_sizes)`** — exhaustive parameter grid search, returns ranked results.
- **`MakerWalkForwardResult.save_trade_log(path)`** — exports the trade log to CSV.

Per-instrument lot sizes are applied correctly: `LOT_SIZES = {"HDFCBANK": 550, "ICICIBANK": 700, "RELIANCE": 500, "TCS": 175}`.

---

## Paper Trader

A live paper trading environment that connects to Dhan's real-time feeds and simulates the maker strategy against actual order flow. No orders are sent to Dhan; all P&L is hypothetical.

### Architecture

```
main.py
  ├── contracts.py       resolve current security_ids from instrument master CSV
  ├── feed_client.py     depth feed asyncio task (depth-only; no market feed needed)
  │     └── dhan_parser.py   binary packet parser
  └── broker.py × 4     one PaperBroker per instrument
        └── logger.py    append-only CSV writes (trades, orders, PnL snapshots)
```

On SIGTERM (sent by the stop timer at 15:35 IST), open positions are force-closed at the last known mid price before the process exits.

### Contract Resolution

`paper_trader/contracts.py` reads `INSTRUMENT_MASTER_PATH` from the shared `.env` (the same file the collector uses) and resolves the current front-month futures `security_id` for each instrument at startup. No manual updates required on monthly expiry rolls — the instrument master is refreshed daily by the collector.

### Systemd Units

Deployed on the same VPS as the data collector. The paper trader symlinks the collector's `.env` to reuse auth credentials and file paths.

| Timer | UTC | IST | Purpose |
|---|---|---|---|
| `argus-paper-trader-start` | 03:40 | 09:10 | Start process before market open |
| `argus-paper-trader-stop` | 10:05 | 15:35 | Graceful stop after market close |
| `argus-paper-trader-report` | 10:20 | 15:50 | Email daily P&L report |

The main service (`argus-paper-trader.service`) has `Restart=on-failure` — it recovers from mid-session crashes automatically. The stop timer sends SIGTERM, which triggers clean shutdown (not a failure restart).

### Daily Report

`paper_trader/report.py` runs at 15:50 IST via systemd. It reads today's `paper_trades.csv`, computes trade-level statistics (n_trades, net P&L, win rate, avg net, exit method breakdown, per-instrument breakdown), and emails the report to the configured address via Gmail SMTP.

### Deployment

See `paper_trader/DEPLOY.md` for the full setup guide. Summary:

```bash
# On VPS (first time)
git clone <repo> /home/ubuntu/paper-trader
cd /home/ubuntu/paper-trader
python3 -m venv venv && venv/bin/pip install -r paper_trader/requirements.txt
ln -s /home/ubuntu/collector-dhan/.env .env
sudo cp paper_trader/systemd/*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now argus-paper-trader-{start,stop,report}.timer

# After pushing changes
ssh lightsail-mumbai "cd /home/ubuntu/paper-trader && git pull"
```

### Paper Trading Success Criteria (20-day evaluation)

| Metric | Target |
|---|---|
| Fill rate | ≥ 6% of posted orders |
| Win rate | ≥ 60% |
| Net P&L per trade | ≥ ₹15 |

---

## Feature Library (`research/features/depth_features.py`)

All features are vectorised (no row-by-row loops). Rolling windows are in **packets**, not seconds.

> **Calibration rule:** A5 and A6 require a size threshold calibrated from data. Always pass `size_threshold` and `order_size` from the **previous day's** `median(bid_qty_01)` to avoid look-ahead bias.

| Feature | Columns | ICIR (h=10) | Verdict |
|---|---|---|---|
| **A6** Liquidity Half-Life | `a6_bid_shallowing`, `a6_ask_shallowing` | ±3.1 | **Core signal** |
| **A5** Level Activation | `a5_condensation_signal` | +2.12 | Strong confirmation |
| **A3** Book Symmetry Break | `a3_symmetry_break` | — | Confirmation gate |
| **A2** Order Fragmentation | `a2_frag_bid` | — | Weak |
| **A1** Institutional Gradient | `a1_gradient_asymmetry` | -0.48 | **Dropped** |
| **A4** Gravity Center Migration | `a4_cog_divergence` | -0.40 | **Dropped** |
| **Microprice Deviation** | `micro_deviation` | 6.9 (h=1) | **Primary signal (live)** |

---

## IC Analysis Summary

- **Method:** Spearman rank IC, horizons h=1,5,10,20,50 packets
- **Coverage:** 4 instruments × 15 trading days = 60 day×symbol observations
- **A6 shallowing pair:** directionally consistent across 100% of observations (pct_pos = 0.0 and 1.0). IC is horizon-flat from h=5 to h=50 packets.
- **Microprice deviation:** ICIR 6.9 at h=1, used as the live trading signal due to low latency requirement of the maker strategy.
- **TCS:** materially weaker signal than the banking names — size down.

---

## Tests

| File | Tests | Coverage |
|---|---|---|
| `tests/test_backtester.py` | 28 | Backtesting engine, cost model |
| `tests/test_features.py` | — | Feature library smoke tests |
| `tests/test_maker_engine.py` | — | Maker engine |
| `tests/test_paper_trader.py` | 36 | Signal math, broker state machine, binary parser, contract resolution |

Run all tests:
```bash
python -m pytest tests/ -v
```

---

## Status

- [x] Data sync pipeline
- [x] Data loader with quality guard
- [x] Feature library A1–A6
- [x] IC/ICIR analysis (A6 + microprice are primary signals)
- [x] Maker strategy backtester with NSE cost model
- [x] Walk-forward validation with per-instrument lot sizes
- [x] OOS validation (train: 15 days, test: 5 days)
- [x] Trade log CSV export
- [x] Paper trader — full environment (broker, logger, report, systemd)
- [x] Paper trader — Dhan websocket connections (depth + market feed)
- [x] Paper trader — auto contract resolution from instrument master
- [x] Paper trader — 36 unit tests (36/36 passing)
- [ ] Deploy paper trader to VPS
- [ ] 20-day live paper trading run
- [ ] Evaluate against success criteria → decision on Phase 2 (live capital)
