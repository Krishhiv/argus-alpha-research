# Argus Alpha Research

Depth-feed-only alpha research and paper-trading system for NSE equity futures. All signals are derived exclusively from the 20-level order book (depth feed). The live system runs a **multi-arm parallel paper-trading harness** — several strategy variants competing head-to-head on one shared feed (see [Multi-Arm Harness](#multi-arm-harness)).

---

## Project Phases

| Phase | Status | What |
|---|---|---|
| 🏕️ **Basecamp** | **running** | 15-day multi-arm paper-trading data-gathering run (7 arms, one shared feed; config frozen) |
| 🔭 **Basecamp Recon** | next | analyse & interpret Basecamp (overfitting-disciplined ranking, regime attribution), then improve/optimise — see [`BASECAMP_RECON.md`](BASECAMP_RECON.md) |
| 🧗 **Expenture** | future | deploy the improved/v2 models and **paper-trade** them to validate |

Real capital is a phase *beyond* Expenture — nothing touches real money until validated twice (Basecamp, then Expenture).

---

## Universe

The live **core** (3 names) is the proven champion universe; the `expanded` arm additionally tests 4 cross-sector **expansion candidates**. Lot sizes are resolved live from the instrument master.

| Instrument | Lot Size | Role |
|---|---|---|
| ICICIBANK | 700 | **Core** — strongest live alpha |
| RELIANCE | 500 | **Core** — strong live alpha |
| HDFCBANK | 550 | **Core** — high win rate, ~break-even (diversifier) |
| SBIN | 750 | Expansion candidate (bank) |
| AXISBANK | 625 | Expansion candidate (bank) |
| BHARTIARTL | 475 | Expansion candidate (telecom — cross-sector diversifier) |
| ITC | 1600 | Expansion candidate (FMCG) — gate-suppressed, rarely trades (low price → spread can't clear fees) |
| TCS | 175 | ⏸️ **Suspended** — ~49% win rate, highest break-even (₹0.72/share), weakest signal |
| BAJFINANCE | — | ❌ **Excluded** — data quality too poor |

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
  config.py                   # default strategy params + runtime config
  signal.py                   # microprice deviation computation
  broker.py                   # PaperBroker (StrategyParams, DayRisk) — order simulator
  arms.py                     # arm registry — the parallel strategy variants
  harness.py                  # multi-arm runtime build + feed fan-out (pure, testable)
  main.py                     # asyncio entry point — one feed → all arms
  contracts.py                # front-month contract + lot-size resolver (instrument master)
  feed_client.py              # Dhan depth websocket client
  dhan_parser.py              # Dhan binary packet parser
  logger.py                   # per-arm CSV event logger (TradeLogger)
  telemetry.py                # live snapshot builder (for the monitor)
  report.py                   # daily multi-arm comparison emailer
  monitor/
    serve_monitor.py          # stdlib HTTP server (/api/monitor + static UI)
    metrics.py                # per-arm realized metrics from the CSVs
    dashboard/                # terminal UI: index.html, app.js, styles.css
  logs/arms/<arm>/            # per-arm paper_trades / paper_orders / paper_pnl CSVs
  systemd/                    # 8 unit files (trader timers + always-on monitor)
  MONITOR.md  DEPLOY.md  requirements.txt
tests/
  test_backtester.py          # 28 — backtesting engine, cost model
  test_features.py            # feature library smoke tests
  test_maker_engine.py        # maker engine
  test_paper_trader.py        # 58 — signal, broker, gate, stop, breaker, parser, contracts
  test_monitor.py             # 14 — realized metrics, multi-arm payload merge
  test_harness.py             # 10 — arm build, fan-out, per-arm isolation
open_monitor.sh               # laptop launcher: tunnel + open the monitor (one command)
run_oos_validation.py         # out-of-sample validation runner
BASECAMP_RECON.md             # the Recon-phase analysis/research plan
```

---

## Signal: Microprice Deviation

The primary trading signal is **microprice deviation** from the L1 order book:

```
microprice       = (bid_price × ask_qty + ask_price × bid_qty) / (bid_qty + ask_qty)
micro_deviation  = microprice − mid
```

A large positive deviation indicates bid-side pressure (bullish); large negative indicates ask-side pressure (bearish). ICIR of **6.9** at h=1 packet in in-sample analysis.

**Entry:** gated by the economic edge gate (see Maker Strategy), with `|micro_deviation| ≥ 0.15` as a signal floor.  
**Signal column:** `micro_deviation` (computed in `paper_trader/signal.py` and `research/features/depth_features.py`)

---

## Maker Strategy

Passive limit order strategy that earns the spread rather than paying it.

**Entry — two gates:**
1. **Signal:** a fresh microprice-deviation cross above the floor `ENTRY_THRESHOLD = 0.15`. "Fresh cross" requires `prev_abs_sig < threshold` so the strategy does not re-enter on a sustained signal.
2. **Economic edge gate (primary filter):** post only when the half-spread the maker would capture covers its per-share round-trip fee with margin:

   ```
   spread / 2  ≥  EDGE_MARGIN × (round_trip_fee / qty)
   ```

   The round-trip fee is computed live from the current mid via the same fee model used for P&L, so the gate is **per-instrument, price-aware, and self-calibrating**. A flat rupee threshold is meaningless across instruments whose price, spread, and per-share fees differ 3×. `EDGE_MARGIN = 1.0` (half-spread must at least cover fees). On entry the order is posted at the current L1 bid (BUY) or ask (SELL).

**Fill detection (depth-only):**
- BUY fills when `bid_price_01 < posted_price` (aggressive sellers consumed our level). SELL fills when `ask_price_01 > posted_price`.
- A fill is accepted only if `qty_consumed ≥ QUEUE_FILL_MIN_FRAC × queue_ahead` (0.10) — guards against noise bounces that never consumed real queue depth.
- Matches the backtester's fill model. No market feed connection is needed.

**Exit (maker):** After a minimum hold of `MIN_HOLD_PKTS = 10` packets (~4 s, prevents same-tick exits), post a passive limit at the current ask (long) or bid (short). Exit fires when the book moves through the posted exit price.

**Exit (taker fallback):** If the position is held for `MAX_HOLD_PACKETS = 250` packets (~100 s) without a passive exit, exit at mid ± 1 tick. Replay across June 1–3 showed a maker needs time to fill its favourable exit — cutting early just dumps trades into the loss bucket. 100 s captures nearly all of that gain with less tail exposure than 160 s.

**Exit (hard stop):** If the position runs `STOP_LOSS_TICKS = 12` ticks (~₹0.60) adverse, exit at market immediately. A deliberately *wide* disaster-stop: tight stops backfire (they cut recoverable wobbles), so this only fires on genuine adverse runs, capping a single trade to ~−0.23% of ₹5L.

**Cooldown:** `ORDER_TIMEOUT_PKTS = 10` packets between a cancel/exit and the next entry.

**Daily circuit breaker:** A session-wide `DayRisk` governor halts *new* entries once aggregate day P&L breaches `DAILY_LOSS_LIMIT = −₹20,000` (open positions still close). This is a catastrophe/bug backstop, **not** a daily risk control — the strategy mean-reverts intraday (every June 1–3 day recovered from a −₹3k to −₹8k trough), so a tight breaker would lock in recoverable drawdowns. The limit sits ~2.5× below the worst observed recoverable dip.

**Robustness:** Zero-price / crossed-book packets (Dhan emits these at the 15:30 IST close) are dropped before any state update, so they cannot trigger spurious fills or corrupt the mid used by the EOD force-close. New entries also stop after `NO_NEW_ENTRY_IST = 15:25`.

**Per-arm configuration:** the values above are the **champion defaults** (`StrategyParams` in `broker.py`). Each parallel arm overrides them — e.g. a different stop, hold, or edge margin — and the `reversal` arm sets `exit_mode="reversal"`, which exits at market the moment the microprice flips against the position (an alternative to the passive maker exit).

---

## Multi-Arm Harness

The live system runs **several strategy variants ("arms") in parallel on one shared depth feed**, as independent risk-free simulations. Because the backtest is unreliable (compacted-data replay missed a live day by ₹11k), live champion-vs-challenger on *identical* data is the only trustworthy way to compare variants — and the harness is the platform for that (and for Recon/Expenture).

**Architecture** (`harness.py` + `main.py`): one Dhan depth connection subscribes to the **union** of all arms' instruments (≤50/connection) and fans each packet out to every arm that trades that symbol. Each arm has its **own** `PaperBroker` set, its own `DayRisk` governor, and **namespaced CSV logs** under `logs/arms/<arm>/`, so arms never contaminate each other. One bad instrument can't crash the run (per-symbol resolution); telemetry can never kill the trader.

**Basecamp arms** — each isolates one open question (all = champion params except where noted):

| Arm | Variant | Question it tests |
|---|---|---|
| `control` | champion, 3 core names | the baseline everything is measured against |
| `expanded` | + SBIN, AXISBANK, ITC, BHARTIARTL | does the edge generalize / diversify? |
| `no_stop` | `STOP_LOSS_TICKS=0` | does the stop help or cut recoverable trades? |
| `wide_stop` | `STOP_LOSS_TICKS=24` | is 12 ticks too tight? |
| `no_icici` | drop ICICIBANK | is ICICIBANK a drag or a high-variance engine? |
| `selective` | `EDGE_MARGIN=1.5` | do fewer, higher-conviction trades win? |
| `reversal` | `exit_mode="reversal"` | does a signal-reversal exit beat the time/stop exit? |

Arms are defined declaratively in `arms.py`. Results are ranked in Basecamp Recon with overfitting discipline (DSR / PBO / n_eff) before any arm is promoted — see [`BASECAMP_RECON.md`](BASECAMP_RECON.md).

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

STT is applied to the sell-side notional, so fees are slightly asymmetric between long and short trades. The economically important quantity is the **per-share break-even move** (round-trip fee ÷ qty) — what the price must move for a trade to clear costs. It varies ~3× across the universe and is what the entry edge gate keys off:

| Instrument | 1-lot round-trip fee | Break-even move/share |
|---|---|---|
| ICICIBANK | ~₹222 | ₹0.32 |
| HDFCBANK | ~₹126 | ₹0.23 |
| RELIANCE | ~₹177 | ₹0.35 |
| TCS (suspended) | ~₹127 | **₹0.72** |

TCS's high break-even (driven by its ~₹2,460 price → STT-heavy) is why its ~50% win-rate signal cannot overcome costs.

---

## Out-of-Sample Validation

Run: `python run_oos_validation.py`

| Period | Dates | Sessions |
|---|---|---|
| Train | 2026-04-24 – 2026-05-15 | 15 days |
| Test (OOS) | 2026-05-18 – 2026-05-22 | 5 days |

**Parameters used:** `entry_threshold=0.20`, `max_hold=500`, `order_timeout=10`, `exit_mode='maker'`, `fresh_cross=True`

> **Live recalibration (2026-05-30):** Queue fill filter added (qty_consumed ≥ 10% of queue_ahead) and minimum 10-packet hold before exit posting, to correct for live tick data being noisier than compacted backtester data.
>
> **Economic edge gate + EOD fix (2026-06-02):** Replaced the flat rupee threshold with a per-instrument economic edge gate (`spread/2 ≥ fee/share`), suspended TCS, reduced `max_hold` to 150, and fixed a critical bug where Dhan's zero-price close-of-session packets corrupted the EOD mid and produced ±₹400k phantom P&L. **Validation:** replaying real June 1–2 depth through the old vs new logic on identical data moved the 2-day net from **−₹12,594 → +₹14,834 (ex-TCS)** and eliminated the phantom trades. Caveat: compacted-data fills are optimistic; live results will be lower.
>
> **Risk tuning + multi-arm harness (2026-06-03 onward):** patient exit (`max_hold` 150 → 250), wide 12-tick disaster-stop, and a −₹20k catastrophe circuit breaker (a tight breaker would lock in the intraday troughs the strategy recovers from). Then the strategy was generalized into the **multi-arm harness** (7 parallel arms) so variants can be compared live on identical data — the trustworthy alternative to the unreliable replay. See [Multi-Arm Harness](#multi-arm-harness).

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
  ├── contracts.py       resolve security_ids + lot sizes from instrument master
  ├── feed_client.py     ONE depth feed (union of all arms' instruments)
  │     └── dhan_parser.py   binary packet parser
  └── harness.py         fan each packet out to every arm
        └── arm × 7       each: own PaperBrokers + DayRisk + namespaced TradeLogger
              └── logs/arms/<arm>/   per-arm trades / orders / PnL CSVs
        └── telemetry.py  combined live snapshot → logs/paper_telemetry.json (1 Hz)
```

On SIGTERM (sent by the stop timer at 15:35 IST), every arm's open positions are force-closed at the last known mid price before the process exits.

### Contract Resolution

`paper_trader/contracts.py` reads `INSTRUMENT_MASTER_PATH` from the shared `.env` (the same file the collector uses) and resolves the current front-month futures `security_id` for each instrument at startup. No manual updates required on monthly expiry rolls — the instrument master is refreshed daily by the collector.

### Systemd Units

Deployed on the same VPS as the data collector. The paper trader symlinks the collector's `.env` to reuse auth credentials and file paths.

| Timer / service | UTC | IST | Purpose |
|---|---|---|---|
| `argus-paper-trader-start` | 03:40 | 09:10 | Start the multi-arm trader before market open |
| `argus-paper-trader-stop` | 10:05 | 15:35 | Graceful stop after market close |
| `argus-paper-trader-report` | 10:20 | 15:50 | Email the daily multi-arm comparison |
| `argus-monitor.service` | — | always-on | Serve the dashboard 24/7 (`Restart=always`, survives reboot) |

The main service (`argus-paper-trader.service`) has `Restart=on-failure` — it recovers from mid-session crashes automatically. The stop timer sends SIGTERM, which triggers clean shutdown (not a failure restart).

### Daily Report

`paper_trader/report.py` runs at 15:50 IST via systemd. It reads each arm's `logs/arms/<arm>/paper_trades.csv` and emails a **multi-arm comparison**: a leaderboard ranked by the day's net, plus per-arm per-instrument and exit-method breakdowns and a running cumulative. Recipient is hardcoded (not read from `.env`).

### Live Monitor

A dependency-free terminal dashboard (`paper_trader/monitor/`) for watching all arms in real time. The running trader writes an atomic combined snapshot (`logs/paper_telemetry.json`, **1 Hz**); `serve_monitor.py` (Python stdlib `http.server`) merges it with the per-arm trade CSVs and serves `/api/monitor` plus a static UI (vanilla HTML/CSS/JS, hand-drawn canvas equity curve — no framework). The UI shows an **arm leaderboard** (ranked by total P&L) with **click-to-drill-in** detail per arm: total/realized/unrealized P&L, win rate, fill rate, day-risk gauge, intraday cumulative-P&L chart, open positions, and per-instrument + exit-method breakdowns.

Runs **always-on** via `argus-monitor.service` (bound to `127.0.0.1`, no laptop needed). Two private access paths — nothing is ever exposed publicly:

- **Laptop:** `./open_monitor.sh` — opens an SSH tunnel and the browser in one command.
- **Phone (vacation/remote):** [Tailscale](https://tailscale.com) **Serve** exposes it over private HTTPS to your own devices only (`https://<host>.<tailnet>.ts.net`). *Serve*, never *Funnel*.

Live data flows during market hours (09:10–15:35 IST); outside them the dashboard shows the day's final realized results with an `OFFLINE` pill. See `paper_trader/MONITOR.md`.

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
sudo systemctl enable --now argus-monitor.service   # always-on dashboard

# After pushing changes
ssh lightsail-mumbai "cd /home/ubuntu/paper-trader && git pull"
```

### Evaluation criteria (assessed per-arm in Basecamp Recon)

Baseline targets per arm, applied *after* the overfitting gates (DSR ≥ 0.95, PBO ≤ 0.2):

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
| `tests/test_paper_trader.py` | 58 | Signal, broker state machine, `StrategyParams`, economic gate, stop, circuit breaker, reversal exit, garbage-packet guard, session cutoff, parser, contracts |
| `tests/test_monitor.py` | 14 | Realized metrics, multi-arm payload merge |
| `tests/test_harness.py` | 10 | Arm build, universe pruning, fan-out, per-arm isolation |

**141 tests passing.** Run all:
```bash
python -m pytest tests/ -v
```

---

## Status

**Research & backtest**
- [x] Data sync pipeline, loader with quality guard
- [x] Feature library A1–A6 + IC/ICIR analysis (A6 + microprice primary)
- [x] Maker backtester (NSE cost model), walk-forward + OOS validation

**Paper trader (live on VPS)**
- [x] Depth-only fill model (market feed dropped — one connection/account limit)
- [x] Microprice signal + economic edge gate + queue filter
- [x] Wide disaster-stop, patient exit, daily circuit breaker, EOD zero-price fix
- [x] Auto contract + lot-size resolution from the instrument master
- [x] **Multi-arm harness** — 7 arms, one shared feed, per-arm logging & risk
- [x] **Live monitor v2** — multi-arm leaderboard, always-on service, phone access via Tailscale
- [x] Deployed & running on the VPS; **141 tests passing**

**Phases**
- [~] 🏕️ **Phase Basecamp** — 15-day multi-arm run *(in progress)*
- [ ] 🔭 **Phase Basecamp Recon** — overfitting-disciplined analysis + regime research + improve (see `BASECAMP_RECON.md`)
- [ ] 🧗 **Phase Expenture** — paper-trade the improved/v2 models
- [ ] Real-capital decision (only after Expenture validates)
