"""
Tier E — queue-aware fill haircut (offline).

Replays the *real* PaperBroker over stored depth twice — once with the optimistic
touch-fill model (Basecamp) and once with the queue-aware exit-fill model
(Expenture I) — and reports how much P&L survives realistic fills. This turns the
"breakeven p* ≈ 67% vs sim 76%" framing into a measured haircut per instrument-day.

It drives the *same* broker code that runs live, so the haircut is apples-to-apples
with the paper run (no separate fill model to keep in sync).

    python -m basecamp_recon.fill_sim --name HDFCBANK --date 2026-06-18 \
        --data-dir /tmp/replay --min-frac 1.0
"""

from __future__ import annotations

import argparse
from dataclasses import replace

import numpy as np

from paper_trader.broker import PaperBroker, StrategyParams
from paper_trader.config import LOT_SIZES
from basecamp_recon.depth_load import load_depth


class _NullLogger:
    """No-op logger — the replay keeps trades in broker.trades, writes nothing."""
    def trade(self, row): pass
    def order_event(self, *a, **k): pass
    def pnl_snapshot(self, *a, **k): pass


def replay(depth_df, params: StrategyParams, *, underlying: str = "SIM",
           lot_size: int | None = None) -> dict:
    """Run one PaperBroker over a depth DataFrame; return trades + summary."""
    ls = lot_size if lot_size is not None else LOT_SIZES.get(underlying, 500)
    br = PaperBroker(underlying, params=params, lot_size=ls, logger=_NullLogger())

    ts = depth_df["collector_received_at"].to_numpy()
    bp = depth_df["bid_price_01"].to_numpy(float)
    bq = depth_df["bid_qty_01"].to_numpy(float)
    ap = depth_df["ask_price_01"].to_numpy(float)
    aq = depth_df["ask_qty_01"].to_numpy(float)
    import pandas as pd
    for i in range(len(depth_df)):
        br.on_depth_packet(ts_utc=pd.Timestamp(ts[i]).to_pydatetime(),
                           bid_price=bp[i], bid_qty=bq[i],
                           ask_price=ap[i], ask_qty=aq[i])
    if len(depth_df):
        br.eod_force_close(ts_utc=pd.Timestamp(ts[-1]).to_pydatetime(), mid=br.last_mid)

    trades = br.trades
    net = float(sum(t["net_pnl"] for t in trades))
    by_exit: dict[str, float] = {}
    for t in trades:
        by_exit[t["exit_method"]] = by_exit.get(t["exit_method"], 0.0) + t["net_pnl"]
    maker = sum(1 for t in trades if t["exit_method"] == "maker_exit")
    return {"trades": trades, "n": len(trades), "net": net,
            "maker_exit_rate": (maker / len(trades)) if trades else float("nan"),
            "by_exit": by_exit}


def haircut(name: str, date: str, *, data_dir: str = "/tmp/replay",
            base: StrategyParams | None = None, min_frac: float = 1.0,
            lot_size: int | None = None) -> dict:
    """Optimistic vs queue-aware replay on one instrument-day → the haircut."""
    base = base or StrategyParams()
    df = load_depth(name, date, data_dir)
    optimistic = replay(df, replace(base, queue_exit_fill=False),
                        underlying=name, lot_size=lot_size)
    realistic  = replay(df, replace(base, queue_exit_fill=True, queue_exit_min_frac=min_frac),
                        underlying=name, lot_size=lot_size)
    o, r = optimistic["net"], realistic["net"]
    return {
        "name": name, "date": date, "packets": len(df),
        "optimistic_net": round(o, 2), "realistic_net": round(r, 2),
        "haircut_rupees": round(o - r, 2),
        "haircut_pct": round(100 * (o - r) / o, 1) if o else float("nan"),
        "optimistic_maker_rate": round(optimistic["maker_exit_rate"], 3),
        "realistic_maker_rate": round(realistic["maker_exit_rate"], 3),
        "optimistic_by_exit": {k: round(v, 0) for k, v in optimistic["by_exit"].items()},
        "realistic_by_exit": {k: round(v, 0) for k, v in realistic["by_exit"].items()},
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Queue-aware fill haircut (offline).")
    p.add_argument("--name", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--data-dir", default="/tmp/replay")
    p.add_argument("--min-frac", type=float, default=1.0)
    p.add_argument("--lot-size", type=int, default=None)
    a = p.parse_args()
    h = haircut(a.name, a.date, data_dir=a.data_dir, min_frac=a.min_frac, lot_size=a.lot_size)
    print(f"\n=== Fill haircut — {h['name']} {h['date']}  ({h['packets']} packets) ===")
    print(f"  optimistic (touch)    net = {h['optimistic_net']:>12,.0f}   "
          f"maker-exit rate {h['optimistic_maker_rate']:.0%}")
    print(f"  realistic  (queue {a.min_frac:g}) net = {h['realistic_net']:>12,.0f}   "
          f"maker-exit rate {h['realistic_maker_rate']:.0%}")
    print(f"  HAIRCUT = {h['haircut_rupees']:>12,.0f}  ({h['haircut_pct']}% of optimistic)")
    print(f"  optimistic by exit: {h['optimistic_by_exit']}")
    print(f"  realistic  by exit: {h['realistic_by_exit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
