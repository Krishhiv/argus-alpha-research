# Argus Alpha Research

Depth-feed-only alpha research system for NSE equity futures. All signals are derived exclusively from the 20-level order book (depth feed). The market feed is excluded — it has too much latency to be usable as a price reference. Midprice `(bid_price_01 + ask_price_01) / 2` is used as the LTP proxy.

---

## Universe

| Instrument | Notes |
|---|---|
| HDFCBANK | Primary development instrument |
| ICICIBANK | |
| RELIANCE | |
| TCS | Weaker A6 signal — size down |
| BAJFINANCE | **Excluded** — data quality too poor |

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
./sync_data.sh           # live sync from VPS
./sync_data.sh --dry-run # preview without transferring
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
    load_data.py         # data loading, derived columns, data quality guard
  features/
    depth_features.py    # A1–A6 feature library
  backtester/
    engine.py            # event-driven backtester, cost model, trade log
  notebooks/
    explore.ipynb        # single-day EDA (HDFCBANK 2026-05-06)
    ic_analysis.ipynb    # IC/ICIR analysis across all instruments and dates
tests/
  test_backtester.py     # 28 smoke tests for the backtesting engine
```

---

## Data Loader (`research/data/load_data.py`)

**`load_depth(underlying, date, session_filter=True, path=None)`**

Loads one day of depth data. Returns a DataFrame with:
- `ts_ist` — IST timestamp (tz-naive)
- `midprice` — `(bid_price_01 + ask_price_01) / 2`
- `spread` — `ask_price_01 - bid_price_01` (discrete, tick-grid multiples of ₹0.05)
- `spread_ticks` — `spread / 0.05`
- `obi` — full-book order book imbalance across all 20 levels
- `obi_l1` — L1-only OBI (quantized and noisy — not used in signals)

Raises `ValueError` if session rows < `MIN_SESSION_ROWS` (corrupt/incomplete file).  
Raises `FileNotFoundError` if the parquet file doesn't exist.

**`safe_load_depth(underlying, date, ...)`** — same as `load_depth` but returns `None` on bad files instead of raising. Use this in any loop over multiple files.

**`load_pair(underlying_a, underlying_b, date)`** — asof-joins two instruments on `ts_ist`.

**`extract_arrays(df, suffix="")`** — returns dict of `(N, 20)` numpy arrays for fast vectorised feature computation.

---

## Feature Library (`research/features/depth_features.py`)

All features are vectorised (no row-by-row loops). Rolling windows are in **packets**, not seconds.

> **Calibration rule:** A5 and A6 require a size threshold calibrated from data. Always pass `size_threshold` and `order_size` from the **previous day's** `median(bid_qty_01)` to avoid look-ahead bias. Never let these fall back to same-day computation in live or backtest code.

| Feature | Columns | Description |
|---|---|---|
| **A1** Institutional Footprint Gradient | `a1_gradient_asymmetry` | Linear slope of avg order size across levels 1–20; asymmetry between bid and ask velocity. **Dropped — ICIR < 0.5.** |
| **A2** Order Fragmentation | `a2_frag_bid`, `a2_frag_ask` | Entropy of order count distribution across levels. High entropy = fragmented book. `a2_frag_bid` is useful; `a2_frag_ask` is noisy. |
| **A3** Book Symmetry Break | `a3_symmetry_break`, `a3_symmetry_break_top`, `a3_symmetry_break_deep` | Compares bid vs ask quantity distribution shape. `a3_symmetry_break` (full book) is the useful variant. |
| **A4** Gravity Center Migration | `a4_cog_divergence` | Centre-of-gravity of volume across levels. **Dropped — ICIR < 0.5.** |
| **A5** Level Activation Pattern | `a5_condensation_signal` | Counts active (above-threshold) levels; condensation signal = rolling rate of change. Positive = levels activating = bullish. |
| **A6** Liquidity Half-Life | `a6_bid_shallowing`, `a6_ask_shallowing` | Simulates a fixed-size market order; measures how many levels it consumes. Rolling shallowing = book getting thinner over time. **Strongest signal.** |

---

## IC Analysis (`research/notebooks/ic_analysis.ipynb`)

**Methodology:**
- Metric: Spearman rank IC (robust to outliers and non-linearity)
- Forward return horizons: 5, 10, 20, 50 packets
- Coverage: 4 instruments × 15 trading days = 60 day×symbol IC observations (first day per instrument skipped — no previous-day calibration available)
- Look-ahead bias: eliminated by using previous-day `median(bid_qty_01)` to calibrate A5 and A6 thresholds

### Signal Rankings (Horizon = 10 packets)

| Signal | Mean IC | ICIR | Pct Positive | Verdict |
|---|---|---|---|---|
| `a6_bid_shallowing` | -0.069 | -3.10 | 0.00 | **Core — always negative** |
| `a6_ask_shallowing` | +0.061 | +3.16 | 1.00 | **Core — always positive** |
| `a5_condensation_signal` | +0.054 | +2.12 | 0.95 | **Strong confirmation** |
| `obi` | +0.021 | +0.76 | 0.75 | Confirmation gate |
| `a1_gradient_asymmetry` | -0.007 | -0.48 | 0.33 | **Drop** |
| `a4_cog_divergence` | -0.008 | -0.40 | 0.35 | **Drop** |

**Key findings:**
- A6 shallowing pair is the primary alpha. Both legs are perfectly directionally consistent (pct_pos = 0.0 and 1.0) across all 60 day×symbol pairs.
- IC is horizon-flat from h=5 to h=50 packets (~2–25 seconds). Signal does not decay quickly, giving flexibility on hold time.
- TCS shows materially weaker A6 signal than the banking names. Size down or filter separately.
- Time-of-day: A5 and A6 are consistent across morning and afternoon — no session-specific logic needed.

### Signal Combination

**Blend 1 — A6 + A5 (primary composite):**
```
score = 0.60 × (a6_ask_shallowing − a6_bid_shallowing) + 0.40 × a5_condensation_signal
```
Weights are ICIR-proportional (A6 ≈ 3.1, A5 ≈ 2.1).

**Blend 2 — A6 + A5 with OBI gate:**
Same composite score, but only enter if `obi` agrees with direction (positive for long, negative for short). Reduces trade count, targets higher win rate.

---

## Backtester (`research/backtester/engine.py`)

Event-driven, packet-by-packet simulation. No third-party frameworks.

**`CostModel`** — NSE equity futures fee schedule (discount broker rates):

| Cost | Rate |
|---|---|
| Brokerage | ₹20 flat per order (×2 per round trip) |
| STT | 0.0125% — sell side only |
| Exchange charge | 0.002% — both sides |
| SEBI fee | 0.0001% — both sides |
| Stamp duty | 0.002% — buy side only |
| GST | 18% on brokerage + exchange + SEBI |
| Slippage | 1 tick (₹0.05) each way — baked into execution price |

On 1 lot HDFCBANK (~₹9L notional), total round-trip cost ≈ ₹225. Break-even requires ~8 ticks of favourable price movement.

**`Backtester(signal_col, entry_threshold, max_hold, stop_ticks, ...)`**

Key parameters:
- `entry_threshold` — minimum `|score|` to open a position
- `max_hold` — maximum packets to hold (default 20)
- `stop_ticks` — stop loss in ticks from effective entry price (default 4)
- `reversal_threshold` — optional early exit on signal flip (disabled by default)

Exit conditions checked in order: `max_hold` → `stop` → `reversal` → `eod` (force-close at last packet).

**`BacktestResult`** — trade log + cumulative PnL series. Exposes `win_rate`, `profit_factor`, `max_drawdown`, `daily_pnl()` for Sharpe calculation across multiple days.

Run tests: `pyenv exec python -m pytest tests/test_backtester.py -v`

---

## Performance Targets

| Metric | Target |
|---|---|
| Net Sharpe ratio | ≥ 1.5 |
| Win rate | ≥ 52% |
| Profit factor | ≥ 1.3 |
| Max drawdown | < 20% |

---

## Status

- [x] Data sync pipeline (`sync_data.sh`)
- [x] Data loader with data quality guard (`load_data.py`)
- [x] Feature library A1–A6 (`depth_features.py`)
- [x] EDA notebook (`explore.ipynb`)
- [x] IC analysis with look-ahead-free calibration (`ic_analysis.ipynb`)
- [x] Backtesting engine with NSE cost model (`backtester/engine.py`)
- [x] Smoke tests — 28 passing (`tests/test_backtester.py`)
- [ ] Signal combination — compute composite score and verify ICIR improvement
- [ ] Entry / exit parameter tuning in backtester
- [ ] Walk-forward validation across all instruments and dates
