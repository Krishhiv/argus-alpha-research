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

---

## Project Structure

```
research/
  data/
    load_data.py       # data loading and derived columns
  features/
    depth_features.py  # A1–A6 feature library
  notebooks/
    explore.ipynb      # single-day EDA (HDFCBANK 2026-05-06)
    ic_analysis.ipynb  # IC/ICIR analysis across all instruments and dates
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
- Coverage: 4 instruments × ~16 trading days = up to 64 day×symbol IC observations
- Look-ahead bias: eliminated by using previous-day `median(bid_qty_01)` to calibrate A5 and A6 thresholds. First day per instrument is skipped.

### Signal Rankings (Horizon = 10 packets)

| Signal | Mean IC | ICIR | Pct Positive | Verdict |
|---|---|---|---|---|
| `a6_bid_shallowing` | -0.069 | -3.10 | 0.00 | **Core — always negative** |
| `a6_ask_shallowing` | +0.061 | +3.16 | 1.00 | **Core — always positive** |
| `a5_condensation_signal` | +0.054 | +2.12 | 0.95 | **Strong confirmation** |
| `a3_symmetry_break` | +0.016 | +1.15 | 0.90 | Filter |
| `a2_frag_bid` | +0.018 | +0.92 | 0.83 | Filter |
| `obi` | +0.021 | +0.76 | 0.75 | Filter |
| `a1_gradient_asymmetry` | -0.007 | -0.48 | 0.33 | **Drop** |
| `a4_cog_divergence` | -0.008 | -0.40 | 0.35 | **Drop** |
| `a3_symmetry_break_top` | +0.001 | +0.07 | 0.53 | **Drop** |
| `a2_frag_ask` | -0.008 | -0.50 | 0.38 | **Drop** |

**Key findings:**
- A6 shallowing pair is the primary alpha. Both legs are perfectly directionally consistent (pct_pos = 0.0 and 1.0) across all 60 day×symbol pairs.
- IC is horizon-flat from h=5 to h=50 packets (~2–25 seconds). Signal does not decay quickly, giving flexibility on hold time.
- TCS shows materially weaker A6 signal than the banking names. Size down or filter separately.
- Time-of-day: A5 and A6 are consistent across morning and afternoon — no session-specific logic needed.

---

## Signal Interpretation

**Long signal conditions:**
- `a6_ask_shallowing` rising (ask book thinning) — supply drying up
- `a5_condensation_signal` positive (levels activating on bid side)
- `obi` > 0, `a3_symmetry_break` > 0 (optional confirmation)

**Short signal conditions:**
- `a6_bid_shallowing` rising (bid book thinning) — demand drying up
- `a5_condensation_signal` negative
- `obi` < 0, `a3_symmetry_break` < 0 (optional confirmation)

---

## Cost Model (NSE Futures)

| Cost | Rate |
|---|---|
| STT | 0.01% (sell side only) |
| Exchange transaction charge | ~0.002% |
| SEBI fee | ~0.0001% |
| Brokerage | ~0.03% |
| Stamp duty | 0.002% (buy side) |
| Slippage | 1 tick (₹0.05) each way |

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
- [x] Data loader (`load_data.py`)
- [x] Feature library A1–A6 (`depth_features.py`)
- [x] EDA notebook (`explore.ipynb`)
- [x] IC analysis with look-ahead-free calibration (`ic_analysis.ipynb`)
- [ ] Signal combination logic
- [ ] Entry / exit mechanics
- [ ] Backtesting engine
- [ ] Full cost model integration
- [ ] Walk-forward validation
