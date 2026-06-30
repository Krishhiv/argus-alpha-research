"""
The overfitting-aware gauntlet — isolated, self-contained (no dependency on the
maker-era code so alpha_lab stands alone).

n_eff → PSR → DSR → PBO, the discipline every candidate alpha must pass on
cost-realistic, out-of-sample returns. Ported from basecamp_recon.arm_stats; the
functions are general-purpose (not maker-specific).
"""

from __future__ import annotations

import math
from itertools import combinations

import numpy as np
import pandas as pd

EULER_GAMMA = 0.5772156649015329


# ── normal CDF / inverse-CDF (no scipy) ─────────────────────────────────────────

def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse normal CDF — Acklam's rational approximation (|err| < 1.2e-9)."""
    if not 0.0 < p < 1.0:
        return float("-inf") if p <= 0 else float("inf")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# ── core statistics ─────────────────────────────────────────────────────────────

def sharpe(returns, ddof: int = 1) -> float:
    r = np.asarray(returns, dtype=float)
    sd = r.std(ddof=ddof)
    return float(r.mean() / sd) if sd > 0 else 0.0


def autocorr(x, lag: int) -> float:
    x = np.asarray(x, dtype=float)
    n = len(x)
    if lag >= n or x.std() == 0:
        return 0.0
    xc = x - x.mean()
    return float(np.sum(xc[:-lag] * xc[lag:]) / np.sum(xc * xc))


def n_eff(x, max_lag: int | None = None) -> float:
    """Effective sample size: n / (1 + 2 Σ_k rho_k), initial-positive-sequence truncation."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 3 or x.std() == 0:
        return float(n)
    if max_lag is None:
        max_lag = min(n - 1, int(10 * math.log10(n)) + 20)
    s = 0.0
    for k in range(1, max_lag + 1):
        rho = autocorr(x, k)
        if rho <= 0:
            break
        s += rho
    denom = 1.0 + 2.0 * s
    return float(n / denom) if denom > 0 else float(n)


def psr(sr: float, n: int, skew: float, kurt: float, sr_star: float = 0.0) -> float:
    """Probabilistic Sharpe Ratio (Bailey & López de Prado). kurt is non-excess (normal=3)."""
    if n < 2:
        return float("nan")
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return float("nan")
    z = (sr - sr_star) * math.sqrt(n - 1) / math.sqrt(denom)
    return float(norm_cdf(z))


def expected_max_sharpe(sr_std: float, n_trials: int) -> float:
    """E[max SR] over N independent zero-mean trials (Bailey & LdP)."""
    if n_trials < 2 or sr_std <= 0:
        return 0.0
    z1 = norm_ppf(1.0 - 1.0 / n_trials)
    z2 = norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(sr_std * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2))


def deflated_sharpe(sr: float, n: int, skew: float, kurt: float, sr_trials) -> tuple[float, float]:
    """DSR: PSR against SR* = E[max SR] under the spread of trial Sharpes. Returns (DSR, sr_star)."""
    sr_trials = np.asarray(sr_trials, dtype=float)
    sr_std = sr_trials.std(ddof=1) if len(sr_trials) > 1 else 0.0
    sr_star = expected_max_sharpe(sr_std, len(sr_trials))
    return psr(sr, n, skew, kurt, sr_star=sr_star), sr_star


def pbo_cscv(M: pd.DataFrame, s: int = 6) -> dict:
    """Probability of Backtest Overfitting via CSCV (Bailey-Borwein-LdP-Zhu)."""
    M = M.dropna(axis=0, how="any")
    T, N = M.shape
    s = max(2, s - (s % 2))
    while s > 2 and T // s < 1:
        s -= 2
    block_len = T // s
    if block_len < 1 or N < 2:
        return {"pbo": float("nan"), "n_splits": 0}
    blocks = [M.iloc[i*block_len:(i+1)*block_len] for i in range(s)]
    logits = []
    for is_idx in combinations(range(s), s // 2):
        oos_idx = [i for i in range(s) if i not in is_idx]
        best = pd.concat([blocks[i] for i in is_idx]).mean().idxmax()
        oos = pd.concat([blocks[i] for i in oos_idx]).mean()
        omega = oos.rank(ascending=True)[best] / (N + 1)
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        logits.append(math.log(omega / (1.0 - omega)))
    logits = np.array(logits)
    return {"pbo": float(np.mean(logits <= 0.0)), "n_splits": len(logits),
            "median_logit": float(np.median(logits))}
