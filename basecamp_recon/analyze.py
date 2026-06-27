"""
Analysis runner — the regime/trend lens on one instrument-day.

Pipeline:  load depth -> microprice -> Kalman velocity -> regime-label windows.
Produces a summary (regime distribution, velocity per regime) and, as a B1
pre-test, shows that the *labelled* trending windows really are the ones with
strong directional moves (i.e. the lens is identifying regimes correctly).

Run:
    python -m basecamp_recon.analyze --name HDFCBANK --date 2026-06-04
    python -m basecamp_recon.analyze --name SBIN --date 2026-06-04 --out sbin.csv
"""

from __future__ import annotations

import argparse

import numpy as np

from basecamp_recon.depth_load import load_depth
from basecamp_recon.kalman import ConstantVelocityKalman, ema
from basecamp_recon.regime import classify_window


def run(name: str, date: str, *, data_dir: str = "/tmp/replay",
        window: int = 600, q: float = 1e-6, r: float = 1e-2,
        ema_alpha: float = 0.02):
    """Returns (df, windows, summary). df has kf_level, kf_velocity, ema columns."""
    df = load_depth(name, date, data_dir)
    price = df["microprice"].to_numpy()
    dts = df["dt"].to_numpy()

    kf = ConstantVelocityKalman(q=q, r=r)
    level, vel = kf.filter(price, dts)
    df["kf_level"] = level
    df["kf_velocity"] = vel
    df["ema"] = ema(price, ema_alpha)

    # Non-overlapping regime windows across the session.
    windows = []
    for start in range(0, len(price), window):
        seg = price[start:start + window]
        if len(seg) < 30:
            break
        info = classify_window(seg)
        seg_vel = vel[start:start + window]
        info["mean_velocity"] = round(float(np.mean(seg_vel)), 6)
        info["realized_move"] = round(float(seg[-1] - seg[0]), 3)
        info["range"] = round(float(seg.max() - seg.min()), 3)
        windows.append(info)

    # Summary.
    from collections import Counter
    dist = Counter(w["regime"] for w in windows)
    trend_w = [w for w in windows if w["regime"].startswith("trend")]
    chop_w = [w for w in windows if w["regime"] in ("chop", "mean_revert")]
    summary = {
        "name": name, "date": date,
        "packets": len(df),
        "session_secs": round(float(df["dt"].sum()), 0),
        "windows": len(windows),
        "regime_distribution": dict(dist),
        # B1 pre-test: trending windows should show larger |move|/range than choppy ones
        "trend_windows_mean_trendiness": round(
            float(np.mean([abs(w["drift_vol"]) for w in trend_w])), 2) if trend_w else None,
        "chop_windows_mean_trendiness": round(
            float(np.mean([abs(w["drift_vol"]) for w in chop_w])), 2) if chop_w else None,
    }
    return df, windows, summary


def _print_report(name, date, df, windows, summary, out=None):
    print(f"\n=== Regime/trend lens — {name} {date} ===")
    print(f"packets={summary['packets']}  session≈{summary['session_secs']/3600:.1f}h  "
          f"windows={summary['windows']}")
    print(f"regime distribution: {summary['regime_distribution']}")
    print(f"B1 pre-test — mean |trendiness| (|drift|/vol):  "
          f"trend-windows={summary['trend_windows_mean_trendiness']}  "
          f"chop-windows={summary['chop_windows_mean_trendiness']}")
    print("\n  window  regime        vr     hurst  trendiness  mean_vel      move    range")
    for i, w in enumerate(windows):
        print(f"  {i:>5}  {w['regime']:<12} {str(w['vr']):>6} {str(w['hurst']):>7} "
              f"{w['drift_vol']:>10} {w['mean_velocity']:>10} {w['realized_move']:>8} {w['range']:>8}")
    if out:
        cols = ["collector_received_at", "mid", "microprice", "micro_dev",
                "kf_level", "kf_velocity", "ema", "dt"]
        df[cols].to_csv(out, index=False)
        print(f"\nwrote {out}  (plot microprice vs kf_level/kf_velocity vs ema)")


def main() -> int:
    p = argparse.ArgumentParser(description="Regime/trend lens on stored depth.")
    p.add_argument("--name", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--data-dir", default="/tmp/replay")
    p.add_argument("--window", type=int, default=600, help="packets per regime window")
    p.add_argument("--q", type=float, default=1e-6, help="Kalman velocity process noise")
    p.add_argument("--r", type=float, default=1e-2, help="Kalman measurement noise")
    p.add_argument("--out", default=None, help="optional CSV path for plotting")
    a = p.parse_args()
    df, windows, summary = run(a.name, a.date, data_dir=a.data_dir,
                               window=a.window, q=a.q, r=a.r)
    _print_report(a.name, a.date, df, windows, summary, out=a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
