"""
Tier A - rigorous, overfitting-aware arm evaluation.

The discipline (BASECAMP_RECON.md §0): a multi-arm race makes the *best* arm look
good by luck alone. Before believing any winner we subtract that luck.

Pipeline:  trades -> daily net-PnL per arm -> Sharpe / n_eff / PSR / DSR / PBO,
           plus paired arm-vs-arm diffs and block-bootstrap CIs.

All P&L is `net_pnl` from the trade log, which is already net of *all* fees
(brokerage + STT + exchange + SEBI + stamp + GST) - i.e. STT-adjusted net, as the
plan requires. We never rank on gross.

Key honesty note: Basecamp gave us ~12 clean trading days. Twelve daily
observations is a *small* sample; PSR/DSR/PBO are computed faithfully and will
(correctly) show low statistical power. That underpowered verdict is itself a
finding - it tells us what Expenture must still earn.
"""

from __future__ import annotations

import glob
import math
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

# 12 clean Basecamp data-days. Excluded: 2026-06-19 (NSE holiday), 2026-06-22
# (Dhan depth-feed outage - 25 trades = noise), 2026-06-26 (NSE holiday).
CLEAN_DAYS = [
    "2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12",
    "2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18",
    "2026-06-23", "2026-06-24", "2026-06-25",
]

EULER_GAMMA = 0.5772156649015329


# ── normal CDF / inverse-CDF (no scipy) ─────────────────────────────────────────

def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse normal CDF - Acklam's rational approximation (|err| < 1.2e-9)."""
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


# ── data loading ──────────────────────────────────────────────────────────────

def load_arm_trades(arm: str, data_dir: str = "basecamp_recon/recon_data/arms",
                    clean_only: bool = True) -> pd.DataFrame:
    """Load one arm's trade log; optionally restrict to the clean data-days."""
    paths = glob.glob(f"{data_dir}/{arm}/paper_trades.csv")
    if not paths:
        raise FileNotFoundError(f"no paper_trades.csv for arm {arm} under {data_dir}")
    df = pd.read_csv(paths[0])
    if clean_only:
        df = df[df["date"].isin(CLEAN_DAYS)].reset_index(drop=True)
    return df


def list_arms(data_dir: str = "basecamp_recon/recon_data/arms") -> list[str]:
    return sorted(p.parent.name for p in map(Path, glob.glob(f"{data_dir}/*/paper_trades.csv")))


def daily_pnl(trades: pd.DataFrame) -> pd.Series:
    """Net P&L summed per day, reindexed across all clean days (missing day = 0)."""
    s = trades.groupby("date")["net_pnl"].sum()
    return s.reindex(CLEAN_DAYS).fillna(0.0)


def arm_daily_matrix(arms: list[str], data_dir: str) -> pd.DataFrame:
    """DataFrame: rows = clean days, cols = arms, values = daily net P&L."""
    cols = {a: daily_pnl(load_arm_trades(a, data_dir)) for a in arms}
    return pd.DataFrame(cols)


def entry_fill_rate(arm: str, data_dir: str) -> float:
    """
    Entry-side post→fill rate from the pnl log (counters are arm-wide and reset
    daily, so daily totals = per-day max). NOTE: this is the *entry* fill rate, not
    the maker-*exit* p that gates live PnL - that one is still unmeasurable here.
    """
    paths = glob.glob(f"{data_dir}/{arm}/paper_pnl.csv")
    if not paths:
        return float("nan")
    pl = pd.read_csv(paths[0])
    pl = pl[pl["date"].isin(CLEAN_DAYS)]
    if pl.empty:
        return float("nan")
    per_day = pl.groupby("date")[["n_posts", "n_fills"]].max().sum()
    return float(per_day["n_fills"] / per_day["n_posts"]) if per_day["n_posts"] else float("nan")


# ── core statistics ─────────────────────────────────────────────────────────────

def sharpe(returns: np.ndarray, ddof: int = 1) -> float:
    """Per-period Sharpe (mean/std). Scale-free, so rupee P&L is fine."""
    r = np.asarray(returns, dtype=float)
    sd = r.std(ddof=ddof)
    return float(r.mean() / sd) if sd > 0 else 0.0


def autocorr(x: np.ndarray, lag: int) -> float:
    x = np.asarray(x, dtype=float)
    n = len(x)
    if lag >= n or x.std() == 0:
        return 0.0
    xc = x - x.mean()
    return float(np.sum(xc[:-lag] * xc[lag:]) / np.sum(xc * xc))


def n_eff(x: np.ndarray, max_lag: int | None = None) -> float:
    """
    Effective sample size for an autocorrelated series:
        n_eff = n / (1 + 2 Σ_k rho_k),  summed over significant positive lags.
    We stop at the first non-positive autocorrelation (standard initial-positive-
    sequence truncation) to avoid summing noise. Trades are strongly
    autocorrelated, so n_eff << raw trade count - this is the honest sample size.
    """
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
    """
    Probabilistic Sharpe Ratio (Bailey & López de Prado 2012):
        PSR(SR*) = Φ( (SR - SR*)·sqrt(n-1) / sqrt(1 - g3·SR + (g4-1)/4·SR²) )
    g3 = skew, g4 = kurtosis (non-excess, i.e. normal = 3). Probability the true
    Sharpe exceeds SR*, given track length and the return distribution's moments.
    """
    if n < 2:
        return float("nan")
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return float("nan")
    z = (sr - sr_star) * math.sqrt(n - 1) / math.sqrt(denom)
    return float(_norm_cdf(z))


def expected_max_sharpe(sr_std: float, n_trials: int) -> float:
    """
    E[max SR] across N independent trials with per-trial Sharpe ~ N(0, sr_std²)
    (Bailey & LdP): sr_std·[ (1-γ)·Z(1-1/N) + γ·Z(1-1/(N·e)) ].
    This is the benchmark SR* a winner must beat to be more than luck.
    """
    if n_trials < 2 or sr_std <= 0:
        return 0.0
    z1 = _norm_ppf(1.0 - 1.0 / n_trials)
    z2 = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(sr_std * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2))


def deflated_sharpe(sr: float, n: int, skew: float, kurt: float,
                    sr_trials: np.ndarray) -> tuple[float, float]:
    """
    Deflated Sharpe Ratio: PSR evaluated against SR* = E[max SR] under the actual
    spread of Sharpes we tried. Returns (DSR, sr_star). DSR ≥ 0.95 is the gate.
    """
    sr_trials = np.asarray(sr_trials, dtype=float)
    n_trials = len(sr_trials)
    sr_std = sr_trials.std(ddof=1) if n_trials > 1 else 0.0
    sr_star = expected_max_sharpe(sr_std, n_trials)
    return psr(sr, n, skew, kurt, sr_star=sr_star), sr_star


def pbo_cscv(M: pd.DataFrame, s: int = 6) -> dict:
    """
    Probability of Backtest Overfitting via Combinatorially-Symmetric Cross-
    Validation (Bailey, Borwein, López de Prado, Zhu 2017).

    M: performance matrix, rows = time obs, cols = strategies (arms).
    Partition the rows into s equal blocks; over every C(s, s/2) way of choosing
    half as IS, pick the IS-best arm, look up its OOS rank, map to a logit. PBO =
    fraction of splits where the IS-best arm lands below the OOS median (logit<0).
    """
    M = M.dropna(axis=0, how="any")
    T, N = M.shape
    s = max(2, s - (s % 2))                 # force even
    while s > 2 and T // s < 1:
        s -= 2
    block_len = T // s
    if block_len < 1 or N < 2:
        return {"pbo": float("nan"), "n_splits": 0, "logits": []}
    blocks = [M.iloc[i * block_len:(i + 1) * block_len] for i in range(s)]
    logits = []
    for is_idx in combinations(range(s), s // 2):
        oos_idx = [i for i in range(s) if i not in is_idx]
        is_perf = pd.concat([blocks[i] for i in is_idx]).mean()
        oos_perf = pd.concat([blocks[i] for i in oos_idx]).mean()
        best = is_perf.idxmax()
        # OOS rank of the IS-best arm: omega in (0,1), 1 = best OOS
        ranks = oos_perf.rank(ascending=True)
        omega = ranks[best] / (N + 1)
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        logits.append(math.log(omega / (1.0 - omega)))
    logits = np.array(logits)
    pbo = float(np.mean(logits <= 0.0)) if len(logits) else float("nan")
    return {"pbo": pbo, "n_splits": len(logits), "median_logit": float(np.median(logits))}


# ── paired comparison & bootstrap ────────────────────────────────────────────────

def paired_diff(a: pd.Series, b: pd.Series, n_boot: int = 10000, seed: int = 0) -> dict:
    """
    Paired daily difference a−b (arms share the same market days → paired test is
    far more powerful). Returns mean diff, t-stat, and a bootstrap 95% CI.
    """
    d = (a - b).dropna().to_numpy()
    n = len(d)
    if n < 2:
        return {"mean": float("nan"), "t": float("nan"), "ci95": (float("nan"),) * 2, "n": n}
    mean = float(d.mean())
    se = d.std(ddof=1) / math.sqrt(n)
    t = mean / se if se > 0 else 0.0
    rng = np.random.default_rng(seed)
    boots = rng.choice(d, size=(n_boot, n), replace=True).mean(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return {"mean": mean, "t": float(t), "ci95": (float(lo), float(hi)),
            "p_gt0": float((boots > 0).mean()), "n": n}


def block_bootstrap_sharpe_ci(returns: np.ndarray, block: int = 2,
                              n_boot: int = 10000, seed: int = 0) -> tuple[float, float]:
    """Circular block bootstrap 95% CI for Sharpe (robust to autocorrelation)."""
    r = np.asarray(returns, dtype=float)
    n = len(r)
    if n < block + 1:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    ext = np.concatenate([r, r[:block]])      # circular
    stats = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n, size=n_blocks)
        sample = np.concatenate([ext[s:s + block] for s in starts])[:n]
        stats[i] = sharpe(sample)
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return (float(lo), float(hi))


# ── per-arm summary ──────────────────────────────────────────────────────────────

@dataclass
class ArmSummary:
    arm: str
    trades: int
    n_eff_trades: float
    n_days: int
    total_net: float
    mean_day: float
    std_day: float
    sharpe_day: float
    sharpe_ci95: tuple[float, float]
    skew: float
    kurt: float
    worst_day: float
    best_day: float
    win_rate: float
    fill_rate: float


def summarize_arm(arm: str, data_dir: str) -> ArmSummary:
    tr = load_arm_trades(arm, data_dir)
    dp = daily_pnl(tr)
    r = dp.to_numpy()
    net = tr["net_pnl"].to_numpy()
    # fill rate from per-trade entries: every row is a fill; posts come from pnl log
    wr = float((net > 0).mean()) if len(net) else float("nan")
    return ArmSummary(
        arm=arm,
        trades=len(tr),
        n_eff_trades=round(n_eff(net), 1),
        n_days=int((dp != 0).sum()),
        total_net=float(net.sum()),
        mean_day=float(r.mean()),
        std_day=float(r.std(ddof=1)),
        sharpe_day=sharpe(r),
        sharpe_ci95=block_bootstrap_sharpe_ci(r),
        skew=float(pd.Series(r).skew()),
        kurt=float(pd.Series(r).kurt() + 3.0),     # pandas gives excess; make non-excess
        worst_day=float(r.min()),
        best_day=float(r.max()),
        win_rate=wr,
        fill_rate=entry_fill_rate(arm, data_dir),
    )


# ── Tier C - attribution ─────────────────────────────────────────────────────────

def _agg(df: pd.DataFrame, by) -> pd.DataFrame:
    g = df.groupby(by)
    out = g["net_pnl"].agg(net="sum", trades="count", mean="mean")
    out["win_rate"] = g["net_pnl"].apply(lambda s: (s > 0).mean())
    return out.sort_values("net", ascending=False)


def attribution_by_instrument(trades: pd.DataFrame) -> pd.DataFrame:
    return _agg(trades, "underlying")


def attribution_by_exit(trades: pd.DataFrame) -> pd.DataFrame:
    return _agg(trades, "exit_method")


def attribution_by_hour(trades: pd.DataFrame) -> pd.DataFrame:
    """Net P&L by IST hour of entry (entry_ts is UTC → +5:30)."""
    df = trades.copy()
    ist = pd.to_datetime(df["entry_ts"], utc=True, format="mixed") + pd.Timedelta(hours=5, minutes=30)
    df["ist_hour"] = ist.dt.hour
    return _agg(df, "ist_hour").sort_index()
