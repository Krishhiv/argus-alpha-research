# Alpha Research Handoff — NSE Equity Futures MFT System
**Project:** `mft_collector` | **Author:** Krishhiv | **Date:** May 2026  
**Constraint:** No ML/RL-based strategies. Pure statistical, mathematical, and structural alphas only.

---

## 1. System Context

### Infrastructure
- **Exchange:** NSE equity futures
- **Deployment:** AWS Lightsail Mumbai (Ubuntu 24.04), systemd service
- **Feed latency p99:** ~0.4s (WebSocket, event-driven)
- **Feed rates:** Market feed ~1–5 updates/sec; Depth feed ~15–30 packets/sec during active hours
- **Signal aggregator cadence:** Every 100ms (not per-packet)

### Instrument Universe
| Symbol | Role | Feed Quality |
|---|---|---|
| HDFCBANK | Primary StatArb leg, signal anchor | Cleanest market feed |
| ICICIBANK | Primary StatArb leg | Cleanest market feed |
| BAJFINANCE | Standalone | Solid depth feed |
| TATASTEEL | Standalone | Validate before full use |
| KOTAKBANK | Standalone | Validate before full use |

**Stat-arb pairs:** HDFC/ICICI (~0.82 correlation, cointegrated, HDFC leads ICICI ~2–3s)

### Data Available
- **~15–16 trading days** of recorded data
- **Market Parquet** (~23 cols): `exchange_ts`, `recv_ts`, `security_id`, `ltp`, `ltq`, `ltt`, `atp`, `oi`, `volume`, `latency_us`, `obi_raw`, `obi_ema` (α=0.08), `obi_flip_rate`, `price_z_60s`, `kalman_price`, `trade_class`, `buy_vol_30s`, `sell_vol_30s`, `bid_liq_total`, `ask_liq_total`, `oi_change_rate`, `regime`, `gap_flag`
- **Depth Parquet** (~125 cols): 20×`bid_price`, 20×`bid_size`, 20×`bid_orders`, 20×`ask_price`, 20×`ask_size`, 20×`ask_orders` + metadata (`exchange_ts`, `recv_ts`, `security_id`, `latency_us`, + 1 more)
- **Merge strategy:** `asof` join at research time on `exchange_ts`

### Hard Constraints
- No ML/RL (insufficient data for convergence — ~15 days)
- No HMM training yet (needs ~3–4 weeks minimum)
- No Backtrader/Zipline — custom event-driven backtester only
- STT cost model must be included in all backtests (STT ~69% of round-trip cost)
- Max 1 open position per symbol, max 2 symbols simultaneously
- Avoid 9:15–9:20 open and 3:25–3:30 close windows
- Staleness gate: suppress trade-dependent signals when `recv_ts - last_market_ts > 2.0s`

---

## 2. Cost Model

Every backtest **must** use the following cost model. Do not run P&L analysis without it.

```python
# NSE Equity Futures Round-Trip Cost Model
# Figures approximate — update with current NSE schedule if needed

STT_RATE = 0.0001          # 0.01% on sell side only (futures)
EXCHANGE_TXN_CHARGE = 0.0000188  # NSE transaction charge
SEBI_TURNOVER_FEE = 0.000001
BROKERAGE = 0.0003         # Assume flat ₹20/order → convert to % at avg contract value
STAMP_DUTY = 0.00002       # On buy side only
SLIPPAGE_TICKS = 1         # Minimum 1 tick slippage each way (conservative)
TICK_SIZE = 0.05           # NSE futures standard

def round_trip_cost(entry_price, exit_price, lot_size):
    notional_entry = entry_price * lot_size
    notional_exit = exit_price * lot_size
    stt = STT_RATE * notional_exit
    txn = EXCHANGE_TXN_CHARGE * (notional_entry + notional_exit)
    sebi = SEBI_TURNOVER_FEE * (notional_entry + notional_exit)
    brokerage = BROKERAGE * 2  # entry + exit, flat ₹20 each → use % approximation
    stamp = STAMP_DUTY * notional_entry
    slippage = 2 * SLIPPAGE_TICKS * TICK_SIZE * lot_size
    return stt + txn + sebi + brokerage + stamp + slippage
```

**Rule of thumb:** Net edge per trade must exceed ~0.05–0.08% of notional to be viable after costs.

---

## 3. Backtesting Requirements

### Framework
Custom event-driven backtester. Do **not** use Backtrader, Zipline, or vectorized pandas backtests.

```
Key design requirements:
- Replay depth snapshots in timestamp order (exchange_ts)
- Asof-join market feed state at each depth event
- Position manager: max 1 open per symbol, max 2 symbols simultaneously
- Daily loss limit hard stop
- Time filter: no entries 9:15–9:20 or 3:25–3:30
- Staleness gate: no signal if recv_ts - last_market_ts > 2.0s
```

### Validation
- **Walk-forward only:** train on day N, test on day N+1 (rolling)
- **No look-ahead:** features must use only data available at signal time
- **Purged cross-validation:** embargo = 325 packets between train and test folds
- **Minimum evaluation:** 5 consecutive days of out-of-sample before declaring an alpha viable

### Metrics to Report (for every alpha)
| Metric | Threshold (viable) |
|---|---|
| Net Sharpe (annualised) | ≥ 1.5 |
| Win rate | ≥ 52% |
| Avg trade P&L (net of costs) | > 0 |
| Max drawdown | < 20% |
| Trades per day | ≥ 5 (enough to evaluate) |
| Profit factor | ≥ 1.3 |
| Rolling 20-day Sharpe | Monitor; pause if < 0.5 |

---

## 4. Alpha Catalogue

Each alpha below has: **Hypothesis → Features → Entry/Exit logic → Implementation notes → Research questions.**

---

### A1 — Institutional Footprint Gradient
**Category:** Depth structural  
**Speed tier:** 5–15 pkt/sec (depth)  
**Novel data use:** `bid_orders`, `ask_orders` (order count per level)

**Hypothesis:**  
Average order size at each level (`size / orders`) reveals order fragmentation. Institutional accumulation creates a characteristic *gradient* across levels — large concentrated orders sit deeper, not at touch. The rate of change of this gradient precedes directional moves.

**Features:**
```python
# Average order size per level
avg_bid_size[i] = bid_size[i] / max(bid_orders[i], 1)   # i = 1..20
avg_ask_size[i] = ask_size[i] / max(ask_orders[i], 1)

# Gradient: fit linear slope across levels 1..20
from numpy.polynomial import polynomial as P
bid_gradient = np.polyfit(range(1, 21), avg_bid_size, 1)[0]
ask_gradient = np.polyfit(range(1, 21), avg_ask_size, 1)[0]

# Signal: acceleration of gradient over last N snapshots
bid_gradient_velocity = bid_gradient_t - bid_gradient_{t-N}
ask_gradient_velocity = ask_gradient_t - ask_gradient_{t-N}

# Asymmetry signal
gradient_asymmetry = bid_gradient_velocity - ask_gradient_velocity
```

**Entry:** Long when `gradient_asymmetry > threshold` (bid side steepening faster than ask). Short vice versa.  
**Exit:** Fixed holding period (test 5s, 15s, 30s) OR when gradient_asymmetry crosses zero.

**Research questions:**
- What is the optimal N (lookback snapshots) for velocity?
- Does signal quality differ by time of day?
- Does it work better on HDFC/ICICI (cleaner feeds) vs others?

---

### A2 — Order Fragmentation Regime
**Category:** Depth structural  
**Speed tier:** 5–15 pkt/sec  
**Novel data use:** `bid_orders`, `ask_orders`

**Hypothesis:**  
When smart money wants to hide, it fragments (same total size, more orders). When urgent, it consolidates. The fragmentation index is a *classifier* that modifies how we interpret OBI — not a raw signal itself.

**Features:**
```python
# For top 5 levels
avg_sizes_bid = [bid_size[i] / max(bid_orders[i], 1) for i in range(5)]
avg_sizes_ask = [ask_size[i] / max(ask_orders[i], 1) for i in range(5)]

frag_index_bid = np.std(avg_sizes_bid) / (np.mean(avg_sizes_bid) + 1e-9)
frag_index_ask = np.std(avg_sizes_ask) / (np.mean(avg_sizes_ask) + 1e-9)

# Existing OBI
obi = (bid_size_total - ask_size_total) / (bid_size_total + ask_size_total + 1e-9)
```

**Entry logic:**
- High `frag_index_bid` + rising `obi` → stealth accumulation → **follow** (buy)
- Low `frag_index_bid` + rising `obi` → urgent aggressive buying → **fade** (sell, expect reversion after fill)

**Implementation note:** This is a *conditional* signal. It modifies the interpretation of OBI. Research it as a filter on top of A1 or standalone OBI first.

**Research questions:**
- What thresholds for frag_index define "high" vs "low"?
- Does frag signal invert around news/event times?

---

### A3 — Book Symmetry Break
**Category:** Depth structural  
**Speed tier:** 5–15 pkt/sec  
**Novel data use:** Full 20-level size vectors

**Hypothesis:**  
A healthy book is symmetric — bid and ask sides mirror each other in shape. Symmetry breaks *before* price moves. The *direction* of the break (which side develops unusual shape) and *at which levels* (top vs deep) tells different stories.

**Features:**
```python
import scipy.stats as stats

# Normalize size vectors to distributions
bid_dist = bid_sizes / (bid_sizes.sum() + 1e-9)   # shape (20,)
ask_dist = ask_sizes / (ask_sizes.sum() + 1e-9)

# KL divergence (asymmetric — compute both directions)
kl_bid_from_ask = stats.entropy(bid_dist + 1e-9, ask_dist + 1e-9)
kl_ask_from_bid = stats.entropy(ask_dist + 1e-9, bid_dist + 1e-9)

symmetry_break = kl_bid_from_ask - kl_ask_from_bid
# Positive: bid side developing unusual shape → potential upward pressure
# Negative: ask side unusual → downward pressure

# Also compute separately for top 5 levels vs levels 6-20
# (top break = aggressive; deep break = passive accumulation)
```

**Entry:** `symmetry_break` crosses threshold in direction; confirm with `obi_raw` not contradicting.  
**Exit:** Symmetry_break reverts toward zero, or fixed holding period.

**Research questions:**
- KL divergence vs Wasserstein distance — which is more predictive?
- Top-5 break vs deep-book break — different holding periods?
- Optimal smoothing: raw snapshot vs EMA of symmetry_break?

---

### A4 — Gravity Center Migration
**Category:** Depth physics  
**Speed tier:** 5–15 pkt/sec  
**Novel data use:** Full 20-level price × size

**Hypothesis:**  
The center of mass (COG) of the book — weighted by size across all 20 levels — is a richer quantity than best bid/ask. Its *velocity* and *acceleration* predict price direction. When both bid and ask COGs drift in the same direction, price follows. When they diverge, mean reversion.

**Features:**
```python
# Center of gravity
bid_cog = np.sum(bid_prices * bid_sizes) / (np.sum(bid_sizes) + 1e-9)
ask_cog = np.sum(ask_prices * ask_sizes) / (np.sum(ask_sizes) + 1e-9)

cog_spread = ask_cog - bid_cog           # Should be positive; tightening = pressure
cog_midpoint = (bid_cog + ask_cog) / 2  # Compare to LTP

# Velocity over last N snapshots
bid_cog_vel = bid_cog_t - bid_cog_{t-N}
ask_cog_vel = ask_cog_t - ask_cog_{t-N}

# Signals
cog_alignment = np.sign(bid_cog_vel) == np.sign(ask_cog_vel)  # Both drifting same way
cog_divergence = bid_cog_vel - ask_cog_vel
```

**Entry:**
- `cog_alignment=True` and both moving up → long
- `cog_alignment=True` and both moving down → short
- `cog_divergence` extreme → mean reversion trade

**Research questions:**
- Optimal N for velocity computation?
- COG midpoint vs LTP deviation as a mean-reversion entry trigger?
- Does COG migration lead price by how many packets on average?

---

### A5 — Level Activation Pattern (Topological)
**Category:** Depth topology  
**Speed tier:** 5–15 pkt/sec  
**Novel data use:** Full 20-level presence/absence

**Hypothesis:**  
Not all 20 levels are active at once. The *pattern* of which levels have meaningful liquidity changes over the day. Condensing books (fewer levels active) precede breakouts. Spreading books (more levels active) precede mean reversion. This is purely topological — no prices or sizes, just structure.

**Features:**
```python
SIZE_THRESHOLD = ...  # Calibrate per instrument (e.g. 1 lot = 1 contract)

# Binary activation
bid_active = (bid_sizes > SIZE_THRESHOLD).astype(int)  # shape (20,)
ask_active = (ask_sizes > SIZE_THRESHOLD).astype(int)

# Activation count
bid_depth_count = bid_active.sum()
ask_depth_count = ask_active.sum()

# Condensation rate over last N snapshots
bid_condensation = bid_depth_count_t - bid_depth_count_{t-N}  # negative = condensing
ask_condensation = ask_depth_count_t - ask_depth_count_{t-N}

# Asymmetric condensation
condensation_signal = bid_condensation - ask_condensation
# Bid condensing faster than ask → liquidity pulling in on buy side → potential breakout up
```

**Entry:** Condensation signal exceeds threshold in a direction.  
**Exit:** Activation pattern stabilizes (condensation_rate near zero).

**Research questions:**
- What SIZE_THRESHOLD makes sense per instrument? (Try: median lot size, mean L1 size)
- Does the signal work better at specific times of day?
- Interaction with regime column in market feed?

---

### A6 — Liquidity Half-Life (Tail Risk Signal)
**Category:** Depth impact  
**Speed tier:** 5–15 pkt/sec  
**Novel data use:** Full 20-level cumulative size

**Hypothesis:**  
Place a hypothetical market order of fixed size X. How deep into the book does it consume? Track how this *impact depth* evolves. When it suddenly shallows (same X eats deeper into the book), invisible thinning is occurring — not visible from spread or L1. Price is about to gap.

**Features:**
```python
ORDER_SIZE_X = ...  # Calibrate: try 5x, 10x, 20x median L1 size

def impact_depth(prices, sizes, order_size):
    """Returns level at which cumulative size crosses order_size."""
    cumulative = np.cumsum(sizes)
    levels_consumed = np.searchsorted(cumulative, order_size) + 1
    price_at_depth = prices[min(levels_consumed, 19)]
    slippage = abs(price_at_depth - prices[0])
    return levels_consumed, slippage

bid_depth, bid_slippage = impact_depth(bid_prices, bid_sizes, ORDER_SIZE_X)
ask_depth, ask_slippage = impact_depth(ask_prices, ask_sizes, ORDER_SIZE_X)

# Signal: sudden shallowing
bid_depth_change = bid_depth_t - bid_depth_{t-N}  # Negative = shallowing = thinning
```

**Use:** This is primarily a **risk filter**, not a trade entry:
- If `bid_depth_change < -threshold`: book thinning rapidly → suppress long entries, consider exiting longs
- If `ask_depth_change < -threshold`: book thinning on ask → suppress short entries

**Research questions:**
- What ORDER_SIZE_X is meaningful per instrument? (function of average daily volume)
- How many packets ahead does depth shallowing predict gap moves?
- Use as entry signal for breakout or purely as risk filter?

---

### A7 — Cross-Symbol Book Resonance (StatArb Enhancement)
**Category:** Cross-instrument, StatArb modifier  
**Speed tier:** 5–15 pkt/sec  
**Applicable pair:** HDFC / ICICI  
**Novel data use:** 20-level size vectors from both symbols simultaneously

**Hypothesis:**  
HDFC and ICICI books don't just correlate in price — their *book shapes* may resonate or decouple before the price relationship breaks down. A sudden decoupling in book shape is an early warning that the cointegration relationship is stressed.

**Features:**
```python
from numpy.linalg import norm

# Normalized size shape vectors
hdfc_bid_shape = hdfc_bid_sizes / (hdfc_bid_sizes.sum() + 1e-9)
icici_bid_shape = icici_bid_sizes / (icici_bid_sizes.sum() + 1e-9)

# Cosine similarity
def cosine_sim(a, b):
    return np.dot(a, b) / (norm(a) * norm(b) + 1e-9)

book_resonance_bid = cosine_sim(hdfc_bid_shape, icici_bid_shape)
book_resonance_ask = cosine_sim(hdfc_ask_shape, icici_ask_shape)
book_resonance = (book_resonance_bid + book_resonance_ask) / 2

# EMA of resonance
resonance_ema = exponential_moving_average(book_resonance, alpha=0.1)
resonance_z = (book_resonance - resonance_ema.mean()) / (resonance_ema.std() + 1e-9)
```

**Use as StatArb modifier:**
- `resonance_z` near zero (books in sync) → normal StatArb operation
- `resonance_z` drops sharply → decouple detected → **widen spread entry threshold** or **skip trade**
- `resonance_z` recovers → re-enable normal StatArb

**Research questions:**
- What resonance drop threshold constitutes meaningful decoupling?
- Does low resonance precede spread divergence or convergence?
- Lead-lag: does HDFC book shape change precede ICICI book shape change?

---

### A8 — VWAP Deviation with Book Confirmation
**Category:** Price-based with book confirmation  
**Speed tier:** 1–5 pkt/sec  
**Data used:** Market feed (`atp`, `ltp`) + depth book

**Hypothesis:**  
Price deviating significantly from session VWAP tends to mean-revert. But naked VWAP deviation is noisy. Book confirmation (OBI in the direction of reversion) materially improves signal quality.

**Features:**
```python
# VWAP from market feed (ATP is cumulative average trade price)
vwap = atp  # Dhan's ATP field is session VWAP

vwap_deviation = (ltp - vwap) / vwap
vwap_z = (vwap_deviation - rolling_mean(vwap_deviation, 300)) / (rolling_std(vwap_deviation, 300) + 1e-9)

# Book confirmation
obi = (bid_liq_total - ask_liq_total) / (bid_liq_total + ask_liq_total + 1e-9)
```

**Entry:**
- `vwap_z > 2.0` AND `obi < -0.2` → price above VWAP, ask side heavy → **short** (reversion)
- `vwap_z < -2.0` AND `obi > 0.2` → price below VWAP, bid side heavy → **long** (reversion)

**Exit:** `vwap_z` crosses 0.5 (partial reversion), or time stop (test 30s, 60s, 120s).

**Research questions:**
- Optimal `vwap_z` threshold (2.0 is a starting point)?
- Does book confirmation actually improve Sharpe vs naked VWAP?
- Intraday pattern — does VWAP mean reversion work better in specific sessions?

---

### A9 — OI Divergence Fade
**Category:** Open interest anomaly  
**Speed tier:** 1–5 pkt/sec  
**Data used:** Market feed (`oi`, `ltp`, `oi_change_rate`)

**Hypothesis:**  
Price moving up with declining OI = weak move (short covering, not new longs). Price moving down with declining OI = weak move (long liquidation, not new shorts). Both tend to revert. Price moving in direction with rising OI = trend continuation.

**Features:**
```python
price_change_60s = ltp_t - ltp_{t-60s}
oi_change_60s = oi_t - oi_{t-60s}

# Divergence: price and OI moving opposite
oi_divergence = np.sign(price_change_60s) != np.sign(oi_change_60s)

# Signal strength: magnitude of price move with magnitude of OI decline
divergence_score = price_change_60s * (-oi_change_60s)  # Positive = divergence
```

**Entry:** Fade the price move when `divergence_score > threshold`.  
**Exit:** Fixed 60–120s holding period.

**Note:** OI updates are slower than price — confirm the Dhan feed OI update frequency before using.

**Research questions:**
- How frequently does OI update in the Dhan market feed (per second? per minute?)?
- Is `oi_change_rate` column in market feed already computed, or does it need recomputing from raw OI?

---

### A10 — Spread Breakout with Book Pressure
**Category:** Microstructure event  
**Speed tier:** 15–30 pkt/sec  
**Data used:** Depth feed (L1 bid/ask)

**Hypothesis:**  
A sudden widening of the bid-ask spread combined with one-sided book pressure indicates a directional move is imminent — not random noise.

**Features:**
```python
spread = ask_price[0] - bid_price[0]
spread_z = (spread - rolling_mean(spread, N)) / (rolling_std(spread, N) + 1e-9)

# One-sided pressure via L1 OBI
l1_obi = (bid_size[0] - ask_size[0]) / (bid_size[0] + ask_size[0] + 1e-9)
```

**Entry:**
- `spread_z > 2.0` AND `l1_obi > 0.3` → spread widening, bid dominant → **long** (aggressive sellers hitting thin ask)
- `spread_z > 2.0` AND `l1_obi < -0.3` → spread widening, ask dominant → **short**

**Exit:** Spread reverts to normal (spread_z < 0.5), or fixed time stop (5–15s).

---

### A11 — Kalman Fair Value Deviation
**Category:** Kalman filter, fair value  
**Speed tier:** 1–5 pkt/sec  
**Data used:** Market feed (`kalman_price`, `ltp`)

**Note:** `kalman_price` is already computed and stored in the market feed Parquet. Use it directly.

**Hypothesis:**  
Kalman-filtered price is a smoother estimate of fair value. Deviations of LTP from Kalman price mean-revert.

**Features:**
```python
kalman_deviation = ltp - kalman_price
kalman_dev_z = (kalman_deviation - rolling_mean(kalman_deviation, N)) / (rolling_std(kalman_deviation, N) + 1e-9)
```

**Entry:** `kalman_dev_z > threshold` → short; `< -threshold` → long.  
**Exit:** Deviation crosses zero (full reversion) or ±0.5σ (partial).

**Research question:** What process noise / observation noise was used to compute `kalman_price`? Recheck `indicators.py` in the collector codebase to confirm parameters.

---

### A12 — HDFC→ICICI Lead-Lag Momentum
**Category:** Cross-symbol lead-lag  
**Speed tier:** 1–5 pkt/sec (market feed)  
**Data used:** Market feed for both symbols  
**Applicable pair:** HDFC leads ICICI by ~2–3s

**Hypothesis:**  
HDFC price moves tend to predict ICICI price moves with a ~2–3s lag. A directional move in HDFC that has not yet appeared in ICICI is an entry signal on ICICI.

**Features:**
```python
# HDFC move in last 3s
hdfc_return_3s = (hdfc_ltp_t - hdfc_ltp_{t-3s}) / hdfc_ltp_{t-3s}

# ICICI move in last 3s (for confirmation that it hasn't already moved)
icici_return_3s = (icici_ltp_t - icici_ltp_{t-3s}) / icici_ltp_{t-3s}

# Signal: HDFC moved significantly, ICICI has not yet
lead_lag_signal = hdfc_return_3s - icici_return_3s
```

**Entry:** `lead_lag_signal > threshold` → long ICICI (following HDFC up). Vice versa for short.  
**Exit:** 3–5s holding period (give time for ICICI to catch up).

**Implementation note:** This requires careful timestamp alignment. Use `exchange_ts` not `recv_ts` for alignment to avoid feed latency artifacts.

**Research questions:**
- Is the 2–3s lead confirmed in your actual recorded data? Measure cross-correlation between HDFC and ICICI returns at lags 1–10s.
- Does the lead-lag relationship hold at different times of day?

---

## 5. Research Priority Order

Given 15–16 days of data and no ML, recommended research sequence:

**Phase 1 — Validate data and baseline (Days 1–3)**
1. Confirm KOTAKBANK and TATASTEEL data quality passes 5-day validation
2. Measure actual HDFC→ICICI lead-lag from recorded data (cross-correlation)
3. Confirm OI update frequency from Dhan feed
4. Establish baseline metrics: spread distributions, OBI distributions, book depth stats per instrument

**Phase 2 — Pure statistical alphas (Days 3–7)**
1. **A8 (VWAP Deviation)** — simplest, uses existing `atp`/`ltp` columns, easy to validate
2. **A11 (Kalman Deviation)** — already have `kalman_price` computed, trivial feature engineering
3. **A9 (OI Divergence)** — uses existing columns, straightforward
4. **A12 (Lead-Lag)** — validate the lag structure first before building signal

**Phase 3 — Depth-based novel alphas (Days 7–14)**
1. **A5 (Level Activation)** — computationally simple, purely topological, fast to prototype
2. **A6 (Liquidity Half-Life)** — use as risk filter first, then test as entry signal
3. **A4 (Gravity COG)** — O(20) computation, clean hypothesis
4. **A3 (Book Symmetry Break)** — requires scipy, slightly more complex

**Phase 4 — Order-count alphas (Days 14+)**
1. **A2 (Fragmentation Regime)** — as OBI modifier
2. **A1 (Institutional Gradient)** — full construction
3. **A7 (Cross-Symbol Resonance)** — StatArb enhancement

---

## 6. File Structure Expectations

```
research/
├── data/
│   ├── load_data.py          # Unified loader: asof-join market + depth
│   └── validate_quality.py   # Per-instrument feed quality checks
├── features/
│   ├── depth_features.py     # A1-A7 feature computation
│   ├── market_features.py    # A8-A12 feature computation
│   └── cost_model.py         # Round-trip cost model
├── backtest/
│   ├── engine.py             # Custom event-driven backtester
│   ├── position_manager.py   # Position/risk constraints
│   └── metrics.py            # Sharpe, drawdown, profit factor, etc.
├── alphas/
│   ├── alpha_A1.py           # One file per alpha
│   ├── alpha_A2.py
│   └── ...
└── notebooks/
    └── alpha_analysis.ipynb  # EDA and results
```

---

## 7. Data Loading Pattern

```python
import pandas as pd
import pyarrow.parquet as pq
from pathlib import Path

DATA_ROOT = Path("/mnt/data")  # AWS Lightsail block storage

def load_instrument(security_id: str, date: str):
    """Load and asof-join market + depth feed for one instrument, one day."""
    market = pq.read_table(
        DATA_ROOT / f"market/{security_id}/{date}.parquet"
    ).to_pandas().sort_values("exchange_ts")
    
    depth = pq.read_table(
        DATA_ROOT / f"depth/{security_id}/{date}.parquet"
    ).to_pandas().sort_values("exchange_ts")
    
    # Asof join: for each depth snapshot, attach most recent market state
    merged = pd.merge_asof(
        depth, market,
        on="exchange_ts",
        direction="backward",
        suffixes=("_depth", "_market")
    )
    
    # Staleness gate
    merged["stale"] = (merged["recv_ts_depth"] - merged["recv_ts_market"]) > 2.0
    
    return merged

def load_pair(sec_id_1: str, sec_id_2: str, date: str):
    """Load both instruments for cross-symbol research."""
    df1 = load_instrument(sec_id_1, date)
    df2 = load_instrument(sec_id_2, date)
    # Align on exchange_ts with asof join
    merged = pd.merge_asof(
        df1.sort_values("exchange_ts"),
        df2.sort_values("exchange_ts"),
        on="exchange_ts",
        direction="nearest",
        suffixes=(f"_{sec_id_1}", f"_{sec_id_2}")
    )
    return merged
```

---

## 8. Key Pinned Dependencies

```
websockets==12.0
pyarrow==15.0.2
pandas==2.2.2
numpy==1.26.4
scipy>=1.12.0       # For KL divergence, Wasserstein (A3)
statsmodels==0.14.2  # For ADF, cointegration tests
scikit-learn==1.4.2
```

---

## 9. Important Notes for Claude Code

1. **Do not modify the collector codebase** (`mft_collector/`) during research. Data collection is ongoing. Research is a read-only operation on `/mnt/data`.

2. **All timestamps are in Unix epoch seconds** (float). `exchange_ts` is the NSE exchange timestamp; `recv_ts` is local receive time. Use `exchange_ts` for all signal computation.

3. **Security IDs:** Instruments are stored by Dhan `security_id`, not by symbol name. Maintain a mapping file `instruments.json` → check `config/instruments.json` in the collector for the ID ↔ symbol mapping.

4. **Parquet column naming:** Depth columns are named `bid_price_1` through `bid_price_20`, `bid_size_1` through `bid_size_20`, `bid_orders_1` through `bid_orders_20`, and equivalent for ask. Always confirm column names by running `df.columns` before assuming.

5. **Rolling computations:** Use `pandas` rolling with `min_periods` set. Never compute features on fewer than 10 observations.

6. **No forward-looking features.** Every feature must be computable from data available at `exchange_ts` of the signal. Any lag window must use `t-N` to `t-1`, never `t+1` or beyond.

7. **Division safety:** Always add `1e-9` to denominators when dividing by order counts, sizes, or standard deviations. Zero counts are common for deep levels.

8. **Regime column:** Market feed contains a `regime` column (TRENDING / MEAN_REVERTING / VOLATILE_BREAKOUT / DEAD). Use this to stratify backtest results by regime — don't report aggregate metrics only.

---