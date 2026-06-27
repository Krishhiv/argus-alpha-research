"""
Recon I report — runs the full Tier-A/C/D analysis on the synced arm logs and
prints the deliverables: ranked arm table (with DSR/PBO/n_eff), paired arm-vs-arm
verdicts (A2–A6), attribution (instrument / exit / hour), and correlation/risk.

    python -m basecamp_recon.recon_report
    python -m basecamp_recon.recon_report --data-dir basecamp_recon/recon_data/arms
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from basecamp_recon import arm_stats as A


def _fmt(x, w=10, d=0):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return " " * (w - 3) + "n/a"
    return f"{x:>{w},.{d}f}"


def run(data_dir: str) -> None:
    arms = A.list_arms(data_dir)
    summaries = {a: A.summarize_arm(a, data_dir) for a in arms}
    M = A.arm_daily_matrix(arms, data_dir)
    sr_trials = np.array([summaries[a].sharpe_day for a in arms])

    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", lambda v: f"{v:,.2f}")

    # ── Tier A: ranked table ──────────────────────────────────────────────────
    print("=" * 96)
    print("RECON I  —  TIER A: RIGOROUS ARM EVALUATION")
    print(f"clean data-days = {len(A.CLEAN_DAYS)}  ({A.CLEAN_DAYS[0]} … {A.CLEAN_DAYS[-1]})")
    print("=" * 96)
    rows = []
    for a in arms:
        s = summaries[a]
        dsr, sr_star = A.deflated_sharpe(s.sharpe_day, s.n_days, s.skew, s.kurt, sr_trials)
        psr0 = A.psr(s.sharpe_day, s.n_days, s.skew, s.kurt, sr_star=0.0)
        rows.append({
            "arm": a, "total_net": s.total_net, "mean_day": s.mean_day,
            "sharpe": s.sharpe_day, "sr_lo": s.sharpe_ci95[0], "sr_hi": s.sharpe_ci95[1],
            "trades": s.trades, "n_eff": s.n_eff_trades, "WR%": 100 * s.win_rate,
            "fill%": 100 * s.fill_rate, "worst_day": s.worst_day,
            "PSR>0": psr0, "DSR": dsr,
        })
    tbl = pd.DataFrame(rows).sort_values("total_net", ascending=False)
    print(f"\n  multiple-testing benchmark SR* (E[max Sharpe] over {len(arms)} arms) = {sr_star:.3f}")
    print(f"  promotion gates:  DSR ≥ 0.95   PBO ≤ 0.20\n")
    hdr = (f"  {'arm':<10}{'total_net':>12}{'mean_day':>11}{'sharpe':>8}"
           f"{'sr_ci95':>16}{'trades':>8}{'n_eff':>8}{'WR%':>7}{'fill%':>7}"
           f"{'worst_day':>11}{'PSR>0':>7}{'DSR':>7}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for _, r in tbl.iterrows():
        print(f"  {r['arm']:<10}{_fmt(r['total_net'],12)}{_fmt(r['mean_day'],11)}"
              f"{r['sharpe']:>8.2f}"
              f"   [{r['sr_lo']:>4.1f},{r['sr_hi']:>4.1f}]"
              f"{int(r['trades']):>8}{r['n_eff']:>8.1f}{r['WR%']:>7.1f}{_fmt(r['fill%'],7,1)}"
              f"{_fmt(r['worst_day'],11)}{r['PSR>0']:>7.2f}{r['DSR']:>7.2f}")

    # ── PBO across the arm race ───────────────────────────────────────────────
    pbo = A.pbo_cscv(M, s=6)
    print(f"\n  PBO (CSCV, {pbo['n_splits']} splits) = {pbo['pbo']:.2f}   "
          f"median OOS-logit = {pbo.get('median_logit', float('nan')):.2f}")

    # ── Tier A2–A6: paired arm-vs-arm verdicts ────────────────────────────────
    print("\n" + "=" * 96)
    print("PAIRED ARM-VS-ARM (daily, same market days)  —  mean diff [95% bootstrap CI], P(>0)")
    print("=" * 96)
    pairs = [
        ("A2 stop helps?",      "no_stop",   "control"),
        ("A2 wide vs tight",    "wide_stop", "control"),
        ("A3 icici a drag?",    "no_icici",  "control"),
        ("A4 new names earn?",  "expanded",  "control"),
        ("A5 selectivity?",     "selective", "control"),
        ("A6 reversal exit?",   "reversal",  "control"),
    ]
    for label, a, b in pairs:
        if a not in M or b not in M:
            continue
        d = A.paired_diff(M[a], M[b])
        lo, hi = d["ci95"]
        verdict = "✓ real" if lo > 0 else ("✗ worse" if hi < 0 else "~ inconclusive")
        print(f"  {label:<20} {a:>10} − {b:<10} "
              f"{d['mean']:>+9,.0f}  [{lo:>+8,.0f}, {hi:>+8,.0f}]  "
              f"P(>0)={d['p_gt0']:.2f}  {verdict}")

    # ── Tier C: attribution (control + expanded) ──────────────────────────────
    for a in ("control", "expanded"):
        if a not in arms:
            continue
        tr = A.load_arm_trades(a, data_dir)
        print("\n" + "=" * 96)
        print(f"TIER C — ATTRIBUTION: {a}")
        print("=" * 96)
        print("\n  by instrument:")
        print(A.attribution_by_instrument(tr).to_string())
        print("\n  by exit method:")
        print(A.attribution_by_exit(tr).to_string())
        print("\n  by IST hour:")
        print(A.attribution_by_hour(tr).to_string())

    # ── Tier D: correlation + risk ────────────────────────────────────────────
    print("\n" + "=" * 96)
    print("TIER D — CORRELATION & DAY-TO-DAY RISK")
    print("=" * 96)
    print("\n  arm daily-PnL correlation matrix:")
    print(M.corr().round(2).to_string())
    print("\n  per-arm daily distribution:")
    print(f"  {'arm':<10}{'mean':>10}{'std':>10}{'skew':>8}{'kurt':>8}"
          f"{'worst':>11}{'best':>11}{'sharpe':>8}")
    for a in arms:
        s = summaries[a]
        print(f"  {a:<10}{s.mean_day:>10,.0f}{s.std_day:>10,.0f}{s.skew:>8.2f}"
              f"{s.kurt:>8.2f}{s.worst_day:>11,.0f}{s.best_day:>11,.0f}{s.sharpe_day:>8.2f}")

    # expanded correlation/breadth risk: how much of total is the best single day?
    for a in ("control", "expanded"):
        if a not in M:
            continue
        col = M[a]
        share = col.max() / col.sum() if col.sum() != 0 else float("nan")
        print(f"\n  {a}: best single day = {col.max():,.0f} "
              f"({100*share:.0f}% of total {col.sum():,.0f}); worst = {col.min():,.0f}")


def main() -> int:
    p = argparse.ArgumentParser(description="Recon I full report.")
    p.add_argument("--data-dir", default="basecamp_recon/recon_data/arms")
    a = p.parse_args()
    run(a.data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
