"""
Regime classification — is the market trending, mean-reverting, or chopping?

Two dimensionless (hence cross-instrument-generalisable) diagnostics:

  Variance Ratio  VR(q) = Var(q-period return) / (q · Var(1-period return))
      VR ≈ 1  random walk   ·   VR > 1  trending   ·   VR < 1  mean-reverting

  Hurst exponent  (R/S analysis on log-price)
      H ≈ 0.5 random walk   ·   H > 0.5 persistent/trending   ·   H < 0.5 reverting

classify_window() combines VR with a vol-normalised signed drift (a t-stat-like
"trendiness") into a label. Thresholds are tunable and expressed in self-relative
units so the same rule applies across instruments.
"""

from __future__ import annotations

import numpy as np


def log_returns(prices) -> np.ndarray:
    p = np.asarray(prices, dtype=float)
    return np.diff(np.log(p))


def variance_ratio(returns, q: int) -> float:
    r = np.asarray(returns, dtype=float)
    n = len(r)
    if q < 2 or n < q + 1:
        return float("nan")
    var1 = np.var(r)
    if var1 == 0:
        return float("nan")
    qsum = np.convolve(r, np.ones(q), mode="valid")   # overlapping q-period returns
    return float(np.var(qsum) / (q * var1))


def hurst_rs(prices, min_w: int = 8, max_w: int | None = None) -> float:
    """Hurst via rescaled-range (R/S). Noisy on short series — use directionally."""
    x = np.log(np.asarray(prices, dtype=float))
    n = len(x)
    if n < 2 * min_w:
        return float("nan")
    if max_w is None:
        max_w = n // 2
    logs_w, logs_rs = [], []
    w = min_w
    while w <= max_w:
        k = n // w
        if k < 1:
            break
        ratios = []
        for i in range(k):
            seg = x[i * w:(i + 1) * w]
            z = seg - seg.mean()
            Z = np.cumsum(z)
            R = Z.max() - Z.min()
            S = seg.std()
            if S > 0:
                ratios.append(R / S)
        if ratios:
            logs_w.append(np.log(w))
            logs_rs.append(np.log(np.mean(ratios)))
        w *= 2
    if len(logs_w) < 2:
        return float("nan")
    return float(np.polyfit(logs_w, logs_rs, 1)[0])   # slope ≈ Hurst


def classify_window(prices, q: int = 10,
                    vr_mr: float = 0.7, drift_vol_min: float = 1.5) -> dict:
    """
    Label one window. Returns regime + the diagnostics behind it.
    regime ∈ {trend_up, trend_down, mean_revert, chop, unknown}

    Trend is defined by a strong *directional* move — |drift|/vol ≥ drift_vol_min
    (the "orderly trend" signature). Among low-drift windows, VR ≤ vr_mr flags
    mean-reversion; otherwise it's random chop. (|drift|/vol is the z-score of the
    cumulative move, so ~N(0,1) under a random walk — a real trend runs 3–5.)
    """
    prices = np.asarray(prices, dtype=float)
    r = log_returns(prices)
    if len(r) < q + 1:
        return {"regime": "unknown", "vr": float("nan"),
                "hurst": float("nan"), "drift_vol": float("nan"), "n": len(prices)}

    vr = variance_ratio(r, q)
    H = hurst_rs(prices)
    drift = float(np.log(prices[-1]) - np.log(prices[0]))   # signed window move (log)
    vol = float(r.std() * np.sqrt(len(r)))                  # total-window vol
    drift_vol = drift / vol if vol > 0 else 0.0             # z-score of the move

    if abs(drift_vol) >= drift_vol_min:
        regime = "trend_up" if drift > 0 else "trend_down"
    elif not np.isnan(vr) and vr <= vr_mr:
        regime = "mean_revert"
    else:
        regime = "chop"

    return {"regime": regime,
            "vr": round(vr, 3) if not np.isnan(vr) else None,
            "hurst": round(H, 3) if not np.isnan(H) else None,
            "drift_vol": round(drift_vol, 2),
            "n": len(prices)}
