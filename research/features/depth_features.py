"""
Depth feature library — A1 through A7.

All functions take a DataFrame produced by load_data.load_depth() and return
a new DataFrame with additional feature columns appended. Operations are
vectorised across all rows; no row-by-row Python loops.

Column naming convention:
  a1_*   Institutional Footprint Gradient
  a2_*   Order Fragmentation Regime
  a3_*   Book Symmetry Break
  a4_*   Gravity Center Migration
  a5_*   Level Activation Pattern
  a6_*   Liquidity Half-Life
  a7_*   Cross-Symbol Book Resonance  (pair DataFrames only)

Rolling windows are specified in number of *packets*, not seconds, because
the packet rate is not constant. Set min_periods=10 throughout to avoid
feature values on insufficient history.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import entropy as kl_divergence  # scipy entropy = KL when qk given

LEVELS = 20
TICK_SIZE = 0.05

BID_PRICE_COLS  = [f"bid_price_{i:02d}" for i in range(1, LEVELS + 1)]
BID_QTY_COLS    = [f"bid_qty_{i:02d}"   for i in range(1, LEVELS + 1)]
BID_ORDER_COLS  = [f"bid_orders_{i:02d}" for i in range(1, LEVELS + 1)]
ASK_PRICE_COLS  = [f"ask_price_{i:02d}" for i in range(1, LEVELS + 1)]
ASK_QTY_COLS    = [f"ask_qty_{i:02d}"   for i in range(1, LEVELS + 1)]
ASK_ORDER_COLS  = [f"ask_orders_{i:02d}" for i in range(1, LEVELS + 1)]

LEVEL_IDX = np.arange(1, LEVELS + 1, dtype=float)  # 1..20 for polyfit


# ---------------------------------------------------------------------------
# A1 — Institutional Footprint Gradient
# ---------------------------------------------------------------------------

def add_a1_gradient(df: pd.DataFrame, velocity_window: int = 10) -> pd.DataFrame:
    """
    Fit a linear slope across avg-order-size per level (1..20) for bid and ask.
    Gradient velocity = change in slope over `velocity_window` packets.
    Gradient asymmetry = bid_velocity - ask_velocity.

    New columns:
        a1_bid_gradient, a1_ask_gradient
        a1_bid_grad_vel, a1_ask_grad_vel
        a1_gradient_asymmetry
    """
    bid_prices  = df[BID_PRICE_COLS].to_numpy(float)
    bid_qtys    = df[BID_QTY_COLS].to_numpy(float)
    bid_orders  = np.maximum(df[BID_ORDER_COLS].to_numpy(float), 1)
    ask_qtys    = df[ASK_QTY_COLS].to_numpy(float)
    ask_orders  = np.maximum(df[ASK_ORDER_COLS].to_numpy(float), 1)

    avg_bid = bid_qtys / bid_orders  # (N, 20)
    avg_ask = ask_qtys / ask_orders

    # Linear slope across levels 1..20 via least-squares for each row.
    # polyfit is not vectorisable, so we use the closed-form OLS slope:
    #   slope = cov(x, y) / var(x)  where x = 1..20 (constant across rows)
    x = LEVEL_IDX
    x_mean = x.mean()
    x_var  = ((x - x_mean) ** 2).sum()

    bid_grad = ((avg_bid - avg_bid.mean(axis=1, keepdims=True)) * (x - x_mean)).sum(axis=1) / x_var
    ask_grad = ((avg_ask - avg_ask.mean(axis=1, keepdims=True)) * (x - x_mean)).sum(axis=1) / x_var

    out = df.copy()
    out["a1_bid_gradient"] = bid_grad
    out["a1_ask_gradient"] = ask_grad

    out["a1_bid_grad_vel"] = (
        out["a1_bid_gradient"] - out["a1_bid_gradient"].shift(velocity_window)
    )
    out["a1_ask_grad_vel"] = (
        out["a1_ask_gradient"] - out["a1_ask_gradient"].shift(velocity_window)
    )
    out["a1_gradient_asymmetry"] = out["a1_bid_grad_vel"] - out["a1_ask_grad_vel"]
    return out


# ---------------------------------------------------------------------------
# A2 — Order Fragmentation Regime
# ---------------------------------------------------------------------------

def add_a2_fragmentation(df: pd.DataFrame, top_levels: int = 5) -> pd.DataFrame:
    """
    Fragmentation index = std(avg_order_size) / mean(avg_order_size) across
    the top `top_levels` levels. High value = many small orders (stealth).
    Low value = few large orders (urgency).

    New columns:
        a2_frag_bid, a2_frag_ask
    """
    bid_qtys   = df[BID_QTY_COLS[:top_levels]].to_numpy(float)
    bid_orders = np.maximum(df[BID_ORDER_COLS[:top_levels]].to_numpy(float), 1)
    ask_qtys   = df[ASK_QTY_COLS[:top_levels]].to_numpy(float)
    ask_orders = np.maximum(df[ASK_ORDER_COLS[:top_levels]].to_numpy(float), 1)

    avg_bid = bid_qtys / bid_orders
    avg_ask = ask_qtys / ask_orders

    frag_bid = avg_bid.std(axis=1) / (avg_bid.mean(axis=1) + 1e-9)
    frag_ask = avg_ask.std(axis=1) / (avg_ask.mean(axis=1) + 1e-9)

    out = df.copy()
    out["a2_frag_bid"] = frag_bid
    out["a2_frag_ask"] = frag_ask
    return out


# ---------------------------------------------------------------------------
# A3 — Book Symmetry Break
# ---------------------------------------------------------------------------

def _row_kl(p: np.ndarray, q: np.ndarray) -> np.ndarray:
    """KL divergence D(p||q) row-wise on (N, 20) arrays. Returns shape (N,)."""
    p = p + 1e-9
    q = q + 1e-9
    p = p / p.sum(axis=1, keepdims=True)
    q = q / q.sum(axis=1, keepdims=True)
    return (p * np.log(p / q)).sum(axis=1)


def add_a3_symmetry(df: pd.DataFrame, ema_alpha: float = 0.05) -> pd.DataFrame:
    """
    KL-divergence between normalised bid and ask size distributions.
    symmetry_break > 0: bid side unusual → upward pressure.
    symmetry_break < 0: ask side unusual → downward pressure.

    Also computed separately for top 5 vs deep (6–20) levels.

    New columns:
        a3_kl_bid_from_ask, a3_kl_ask_from_bid
        a3_symmetry_break          (full 20 levels)
        a3_symmetry_break_top      (levels 1–5)
        a3_symmetry_break_deep     (levels 6–20)
        a3_symmetry_break_ema      (EMA smoothed)
    """
    bid = df[BID_QTY_COLS].to_numpy(float)
    ask = df[ASK_QTY_COLS].to_numpy(float)

    kl_b_a = _row_kl(bid, ask)
    kl_a_b = _row_kl(ask, bid)

    bid_top  = bid[:, :5]
    ask_top  = ask[:, :5]
    bid_deep = bid[:, 5:]
    ask_deep = ask[:, 5:]

    sym_top  = _row_kl(bid_top, ask_top)  - _row_kl(ask_top, bid_top)
    sym_deep = _row_kl(bid_deep, ask_deep) - _row_kl(ask_deep, bid_deep)

    out = df.copy()
    out["a3_kl_bid_from_ask"]    = kl_b_a
    out["a3_kl_ask_from_bid"]    = kl_a_b
    out["a3_symmetry_break"]     = kl_b_a - kl_a_b
    out["a3_symmetry_break_top"] = sym_top
    out["a3_symmetry_break_deep"]= sym_deep
    out["a3_symmetry_break_ema"] = (
        out["a3_symmetry_break"].ewm(alpha=ema_alpha, adjust=False).mean()
    )
    return out


# ---------------------------------------------------------------------------
# A4 — Gravity Center Migration
# ---------------------------------------------------------------------------

def add_a4_cog(df: pd.DataFrame, velocity_window: int = 10) -> pd.DataFrame:
    """
    Center-of-gravity = weighted average price across all 20 levels.
    Velocity = change in COG over `velocity_window` packets.
    Alignment = both bid and ask COG drifting the same direction.

    New columns:
        a4_bid_cog, a4_ask_cog
        a4_cog_spread       (ask_cog - bid_cog, should be > 0)
        a4_cog_midpoint     (midpoint of COGs vs L1 midprice)
        a4_bid_cog_vel, a4_ask_cog_vel
        a4_cog_aligned      (bool: both velocities same sign)
        a4_cog_divergence   (bid_vel - ask_vel)
    """
    bid_prices = df[BID_PRICE_COLS].to_numpy(float)
    bid_qtys   = df[BID_QTY_COLS].to_numpy(float)
    ask_prices = df[ASK_PRICE_COLS].to_numpy(float)
    ask_qtys   = df[ASK_QTY_COLS].to_numpy(float)

    bid_cog = (bid_prices * bid_qtys).sum(axis=1) / (bid_qtys.sum(axis=1) + 1e-9)
    ask_cog = (ask_prices * ask_qtys).sum(axis=1) / (ask_qtys.sum(axis=1) + 1e-9)

    out = df.copy()
    out["a4_bid_cog"]      = bid_cog
    out["a4_ask_cog"]      = ask_cog
    out["a4_cog_spread"]   = ask_cog - bid_cog
    out["a4_cog_midpoint"] = (bid_cog + ask_cog) / 2 - df["midprice"]

    out["a4_bid_cog_vel"]    = out["a4_bid_cog"] - out["a4_bid_cog"].shift(velocity_window)
    out["a4_ask_cog_vel"]    = out["a4_ask_cog"] - out["a4_ask_cog"].shift(velocity_window)
    out["a4_cog_aligned"]    = np.sign(out["a4_bid_cog_vel"]) == np.sign(out["a4_ask_cog_vel"])
    out["a4_cog_divergence"] = out["a4_bid_cog_vel"] - out["a4_ask_cog_vel"]
    return out


# ---------------------------------------------------------------------------
# A5 — Level Activation Pattern
# ---------------------------------------------------------------------------

def _infer_lot_size(df: pd.DataFrame) -> float:
    """
    Infer lot size as the minimum positive L1 bid qty observed.
    Lot size is a fixed NSE-defined constant — does not change intraday.
    """
    positive = df["bid_qty_01"][df["bid_qty_01"] > 0]
    return float(positive.min()) if len(positive) > 0 else 550.0


def add_a5_activation(
    df: pd.DataFrame,
    size_threshold: int | None = None,
    condensation_window: int = 20,
) -> pd.DataFrame:
    """
    Binary activation: level is "active" if qty > size_threshold.
    Condensation rate = change in active-level count over `condensation_window` packets.

    size_threshold : explicitly pass this to avoid any look-ahead.
        Best practice: pass the previous day's median(bid_qty_01).
        If None, falls back to lot size (minimum positive L1 qty) — causal,
        but may be too low to produce meaningful variation in active_count.
        # TODO: always pass size_threshold explicitly in the backtester.

    New columns:
        a5_bid_active_count, a5_ask_active_count
        a5_bid_condensation, a5_ask_condensation   (negative = condensing)
        a5_condensation_signal  (bid - ask condensation: bid condensing → bullish)
    """
    bid_qtys = df[BID_QTY_COLS].to_numpy(float)
    ask_qtys = df[ASK_QTY_COLS].to_numpy(float)

    threshold = size_threshold if size_threshold is not None else _infer_lot_size(df)

    bid_active = (bid_qtys > threshold).sum(axis=1).astype(float)
    ask_active = (ask_qtys > threshold).sum(axis=1).astype(float)

    out = df.copy()
    out["a5_bid_active_count"] = bid_active
    out["a5_ask_active_count"] = ask_active

    bid_s = pd.Series(bid_active, index=df.index)
    ask_s = pd.Series(ask_active, index=df.index)

    out["a5_bid_condensation"]    = bid_s - bid_s.shift(condensation_window)
    out["a5_ask_condensation"]    = ask_s - ask_s.shift(condensation_window)
    out["a5_condensation_signal"] = out["a5_bid_condensation"] - out["a5_ask_condensation"]
    return out


# ---------------------------------------------------------------------------
# A6 — Liquidity Half-Life (Impact Depth)
# ---------------------------------------------------------------------------

def _impact_depth_vectorised(
    qtys: np.ndarray, order_size: float
) -> tuple[np.ndarray, np.ndarray]:
    """
    For each row, find the level at which cumulative qty first exceeds order_size.
    Returns (levels_consumed, residual_qty_at_that_level).

    qtys : shape (N, 20)
    """
    cumsum = np.cumsum(qtys, axis=1)  # (N, 20)
    exceeded = cumsum >= order_size   # (N, 20) bool
    # argmax on bool gives first True; if never True, gives 0 (but capped at 19)
    levels = np.where(exceeded.any(axis=1), exceeded.argmax(axis=1) + 1, LEVELS)
    return levels.astype(float)


def add_a6_impact_depth(
    df: pd.DataFrame,
    order_size: float | None = None,
    order_size_lots: int = 20,
    shallowing_window: int = 20,
) -> pd.DataFrame:
    """
    Simulate hitting the book with `order_size` lots and measure how many
    levels are consumed. When the book thins, the same order eats deeper.

    order_size      : Explicit qty in units. Pass this for full control.
                      Best practice: pass previous day's median(bid_qty_01) × 10.
    order_size_lots : Used only when order_size is None. order_size is set to
                      order_size_lots × lot_size (lot_size = min positive L1 qty).
                      Default 20 lots — causal, but calibrate against previous-day
                      data for production use.
    # TODO: always pass order_size explicitly in the backtester.

    New columns:
        a6_bid_impact_depth, a6_ask_impact_depth
        a6_bid_shallowing, a6_ask_shallowing   (negative = thinning = danger)
        a6_book_thin_flag   (True when either side shallowing rapidly)
    """
    bid_qtys = df[BID_QTY_COLS].to_numpy(float)
    ask_qtys = df[ASK_QTY_COLS].to_numpy(float)

    if order_size is None:
        lot_size = _infer_lot_size(df)
        order_size = lot_size * order_size_lots

    bid_depth = _impact_depth_vectorised(bid_qtys, order_size)
    ask_depth = _impact_depth_vectorised(ask_qtys, order_size)

    out = df.copy()
    out["a6_bid_impact_depth"] = bid_depth
    out["a6_ask_impact_depth"] = ask_depth

    bid_d = pd.Series(bid_depth, index=df.index)
    ask_d = pd.Series(ask_depth, index=df.index)

    out["a6_bid_shallowing"] = bid_d - bid_d.shift(shallowing_window)
    out["a6_ask_shallowing"] = ask_d - ask_d.shift(shallowing_window)

    # Flag when either side is thinning beyond 2 levels in the window
    THINNING_THRESHOLD = 2
    out["a6_book_thin_flag"] = (
        (out["a6_bid_shallowing"] < -THINNING_THRESHOLD)
        | (out["a6_ask_shallowing"] < -THINNING_THRESHOLD)
    )
    return out


# ---------------------------------------------------------------------------
# Microprice (Stoikov 2018) — queue-weighted midprice
# ---------------------------------------------------------------------------

def add_microprice(df: pd.DataFrame) -> pd.DataFrame:
    """
    Microprice weights the L1 prices by OPPOSING-side queue size:

        micro = (bid_price × ask_qty + ask_price × bid_qty) / (bid_qty + ask_qty)

    When ask_qty is small (depleted), micro pulls toward ask_price → upward bias.
    When bid_qty is small, micro pulls toward bid_price → downward bias.

    `micro_deviation = micro - midprice` is the signed predictor.

    From IC analysis: ICIR 6.9 at h=1, mean IC 0.147 at h=10 — the strongest
    single signal in this codebase.

    New columns:
        microprice, micro_deviation
    """
    bid_p = df["bid_price_01"].to_numpy(float)
    bid_q = df["bid_qty_01"].to_numpy(float)
    ask_p = df["ask_price_01"].to_numpy(float)
    ask_q = df["ask_qty_01"].to_numpy(float)

    total_q = bid_q + ask_q
    micro = (bid_p * ask_q + ask_p * bid_q) / (total_q + 1e-9)

    out = df.copy()
    out["microprice"]      = micro
    out["micro_deviation"] = micro - df["midprice"].to_numpy(float)
    return out


# ---------------------------------------------------------------------------
# Multi-level Order Flow Imbalance (Cont, Kukanov, Stoikov 2014; Kolm 2021)
# ---------------------------------------------------------------------------

def add_ofi_multilevel(
    df: pd.DataFrame,
    n_levels: int   = 10,
    decay:    float = 0.5,
) -> pd.DataFrame:
    """
    Per-level bid/ask flow contribution between packet t-1 and packet t:

        e_b = qty_t            if price moved up    (new buy interest at higher bid)
            = -qty_{t-1}       if price moved down  (cancellations at old bid)
            = qty_t - qty_{t-1} if price unchanged   (net flow at same level)

    Symmetric for ask (sign flipped — lower ask = bullish). Weighted sum across
    top `n_levels` with exponential decay so L1 dominates.

    OFI > 0 → net buying pressure; OFI < 0 → net selling pressure.

    New column: ofi_ml
    """
    ofi_total = np.zeros(len(df))

    for k in range(1, n_levels + 1):
        weight = decay ** (k - 1)
        bp = df[f"bid_price_{k:02d}"].to_numpy(float)
        bq = df[f"bid_qty_{k:02d}"].to_numpy(float)
        ap = df[f"ask_price_{k:02d}"].to_numpy(float)
        aq = df[f"ask_qty_{k:02d}"].to_numpy(float)

        bp_prev = np.roll(bp, 1); bq_prev = np.roll(bq, 1)
        ap_prev = np.roll(ap, 1); aq_prev = np.roll(aq, 1)

        e_b = np.where(bp > bp_prev, bq,
              np.where(bp < bp_prev, -bq_prev, bq - bq_prev))
        e_a = np.where(ap < ap_prev, aq,
              np.where(ap > ap_prev, -aq_prev, aq - aq_prev))

        ofi_total += weight * (e_b - e_a)

    ofi_total[0] = 0  # first packet has no prior reference

    out = df.copy()
    out["ofi_ml"] = ofi_total
    return out


# ---------------------------------------------------------------------------
# Composite signal
# ---------------------------------------------------------------------------

COMPOSITE_ZSCORE_WINDOW = 100  # packets (~30–50 seconds of history)


def _rolling_zscore(s: pd.Series, window: int) -> pd.Series:
    mu  = s.rolling(window, min_periods=20).mean()
    std = s.rolling(window, min_periods=20).std()
    return (s - mu) / (std + 1e-9)


def add_composite(
    df: pd.DataFrame,
    zscore_window: int = COMPOSITE_ZSCORE_WINDOW,
) -> pd.DataFrame:
    """
    Equal-weight composite of A6 shallowing pair and A5 condensation signal.

    Each component is rolling z-scored before combining so that differences
    in raw scale do not give any one signal disproportionate weight.

    Formula:
        composite_eq = zscore(a6_ask_shallowing)
                     - zscore(a6_bid_shallowing)   ← bid shallowing is bearish
                     + zscore(a5_condensation_signal)

    Requires A5 and A6 columns to already be present (call after add_all_features
    or after add_a5_activation + add_a6_impact_depth).

    New column:
        composite_eq
    """
    required = ["a6_ask_shallowing", "a6_bid_shallowing", "a5_condensation_signal"]
    missing  = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"add_composite requires columns: {missing}. Run add_all_features first.")

    out = df.copy()
    raw = (
        _rolling_zscore(out["a6_ask_shallowing"],        zscore_window)
        - _rolling_zscore(out["a6_bid_shallowing"],      zscore_window)
        + _rolling_zscore(out["a5_condensation_signal"], zscore_window)
    )
    # Smooth with EMA (span=zscore_window) to remove packet-to-packet noise and
    # create a slow-moving regime signal. Raw std ≈ √3 ≈ 1.73 is preserved.
    out["composite_eq"] = raw.ewm(span=zscore_window, adjust=False).mean()
    return out


def add_flow_composite(
    df: pd.DataFrame,
    zscore_window: int = COMPOSITE_ZSCORE_WINDOW,
    ema_span:      int = COMPOSITE_ZSCORE_WINDOW,
) -> pd.DataFrame:
    """
    Flow-based composite emphasising micro_deviation (the strongest signal
    found in IC analysis: ICIR 6.9 at h=1, 4.8 at h=10).

    Weighting reflects per-signal ICIR:
        flow_composite = EMA( 3·z(micro_deviation)
                            + 1·z(ofi_ml)
                            + 1·z(a6_ask_shallowing)
                            − 1·z(a6_bid_shallowing) , span)

    Requires `microprice`, `ofi_ml`, and A6 columns to be present.

    New column: flow_composite
    """
    required = ["micro_deviation", "ofi_ml", "a6_ask_shallowing", "a6_bid_shallowing"]
    missing  = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"add_flow_composite requires columns: {missing}")

    out = df.copy()
    z_micro = _rolling_zscore(out["micro_deviation"],   zscore_window)
    z_ofi   = _rolling_zscore(out["ofi_ml"],            zscore_window)
    z_ask   = _rolling_zscore(out["a6_ask_shallowing"], zscore_window)
    z_bid   = _rolling_zscore(out["a6_bid_shallowing"], zscore_window)

    raw = 3.0 * z_micro + 1.0 * z_ofi + 1.0 * z_ask - 1.0 * z_bid
    out["flow_composite"] = raw.ewm(span=ema_span, adjust=False).mean()
    return out


# ---------------------------------------------------------------------------
# A7 — Cross-Symbol Book Resonance (pair DataFrames)
# ---------------------------------------------------------------------------

def add_a7_resonance(
    df: pd.DataFrame,
    ema_alpha: float = 0.1,
    z_window: int = 200,
    suffix_a: str = "_a",
    suffix_b: str = "_b",
) -> pd.DataFrame:
    """
    Cosine similarity between normalised bid (and ask) size shape vectors
    of two instruments loaded via load_pair().

    Low resonance → books decoupling → widen StatArb entry threshold.

    New columns:
        a7_resonance_bid, a7_resonance_ask
        a7_resonance         (average of both sides)
        a7_resonance_ema
        a7_resonance_z       (z-score over rolling `z_window` packets)
    """
    def _get_qty(s):
        return df[[f"bid_qty_{i:02d}{s}" for i in range(1, LEVELS + 1)]].to_numpy(float)

    def _get_ask_qty(s):
        return df[[f"ask_qty_{i:02d}{s}" for i in range(1, LEVELS + 1)]].to_numpy(float)

    def _cosine_sim_rows(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        a_norm = a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)
        b_norm = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-9)
        return (a_norm * b_norm).sum(axis=1)

    bid_a = _get_qty(suffix_a)
    bid_b = _get_qty(suffix_b)
    ask_a = _get_ask_qty(suffix_a)
    ask_b = _get_ask_qty(suffix_b)

    res_bid = _cosine_sim_rows(bid_a, bid_b)
    res_ask = _cosine_sim_rows(ask_a, ask_b)
    res     = (res_bid + res_ask) / 2

    out = df.copy()
    out["a7_resonance_bid"] = res_bid
    out["a7_resonance_ask"] = res_ask
    out["a7_resonance"]     = res

    res_s = pd.Series(res, index=df.index)
    out["a7_resonance_ema"] = res_s.ewm(alpha=ema_alpha, adjust=False).mean()

    rolling = res_s.rolling(window=z_window, min_periods=10)
    out["a7_resonance_z"] = (res_s - rolling.mean()) / (rolling.std() + 1e-9)
    return out


# ---------------------------------------------------------------------------
# Convenience: add all single-instrument features in one call
# ---------------------------------------------------------------------------

def add_all_features(
    df: pd.DataFrame,
    velocity_window: int = 10,
    condensation_window: int = 20,
    shallowing_window: int = 20,
    zscore_window: int = COMPOSITE_ZSCORE_WINDOW,
    size_threshold: int | None = None,
    order_size: float | None = None,
    order_size_lots: int = 20,
) -> pd.DataFrame:
    """
    Apply A1–A6 and composite signal to a single-instrument DataFrame from load_depth().

    To avoid any look-ahead, pass size_threshold and order_size explicitly
    (e.g. derived from previous day's data). If left as None, both fall back
    to the instrument lot size (minimum positive L1 qty), which is a fixed
    NSE constant and is look-ahead-free.

    Adds composite_eq as the final column — the primary trading signal.
    """
    df = add_a1_gradient(df, velocity_window=velocity_window)
    df = add_a2_fragmentation(df)
    df = add_a3_symmetry(df)
    df = add_a4_cog(df, velocity_window=velocity_window)
    df = add_a5_activation(df, size_threshold=size_threshold, condensation_window=condensation_window)
    df = add_a6_impact_depth(df, order_size=order_size, order_size_lots=order_size_lots, shallowing_window=shallowing_window)
    df = add_microprice(df)
    df = add_ofi_multilevel(df)
    df = add_composite(df, zscore_window=zscore_window)
    df = add_flow_composite(df, zscore_window=zscore_window, ema_span=zscore_window)
    return df
