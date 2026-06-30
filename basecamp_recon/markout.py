"""
Adverse-selection / markout analysis — the honest test of the maker edge.

The optimistic paper fills capture 15-25x the spread, which means we're booking
*directional* moves a passive order would not really get. Markout analysis
measures, straight from the stored depth and independent of the fill model:

  Entry markout(h) = dir * (mid(t_fill + h) - fill_price)
      h=0  → the *captured edge* (how favorable our fill was vs the mid; a real
             maker fill sits ~half-spread inside the mid, so this is positive).
      h>0  → adverse selection erodes it: if the market systematically moves
             against us right after we fill, markout decays (or goes negative).
      A maker is being *picked off* when adverse drift > captured edge.

  Mid-to-mid PnL = dir * (mid(exit_ts) - mid(entry_ts)) * qty - fee
      Marks BOTH legs to the mid — zero spread capture, pure directional. If this
      is still positive, the signal has real directional alpha; if it collapses to
      ≤0, the entire paper profit was spread-capture / fill artifact.

Runs on the VPS with the collector venv (needs pyarrow):
    ~/collector-dhan/venv/bin/python -m basecamp_recon.markout \
        --trades ~/paper-trader/paper_trader/logs/arms_basecamp/expanded/paper_trades.csv \
        --depth-root ~/data/tbt-dhan/depth
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import numpy as np
import pandas as pd

CLEAN_DAYS = [
    "2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12",
    "2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18",
    "2026-06-23", "2026-06-24", "2026-06-25",
]
HORIZONS_S = [0, 1, 5, 30, 60]
L1 = ["collector_received_at", "bid_price_01", "ask_price_01"]


def find_depth_file(name: str, date: str, root: str) -> str | None:
    base = f"{root}/trading_date={date}"
    hits = sorted(glob.glob(f"{base}/symbol={name}-*/compacted-*.parquet"))
    if not hits:
        hits = sorted(glob.glob(f"{base}/symbol={name}-*/*.parquet"))
    return hits[0] if hits else None


def load_day_mid(name: str, date: str, root: str) -> pd.DataFrame | None:
    """Clean mid series for one symbol-day: DataFrame[ts(UTC, sorted), mid]."""
    f = find_depth_file(name, date, root)
    if f is None:
        return None
    # a symbol-day may be split into several raw files; concat them all
    files = sorted(glob.glob(str(Path(f).parent / "*.parquet")))
    comp = [p for p in files if "compacted" in Path(p).name]
    files = comp if comp else files
    df = pd.concat([pd.read_parquet(p, columns=L1) for p in files], ignore_index=True)
    bp = df.bid_price_01.to_numpy(float)
    ap = df.ask_price_01.to_numpy(float)
    good = (bp > 0) & (ap > 0) & (ap >= bp)
    df = df.loc[good]
    ts = pd.to_datetime(df.collector_received_at, utc=True)
    out = pd.DataFrame({"ts": ts.to_numpy(),
                        "mid": (df.bid_price_01.to_numpy(float) + df.ask_price_01.to_numpy(float)) / 2.0})
    return out.sort_values("ts").reset_index(drop=True)


def compute(trades_path: str, depth_root: str) -> tuple[pd.DataFrame, dict]:
    tr = pd.read_csv(trades_path)
    tr = tr[tr.date.isin(CLEAN_DAYS)].copy()
    tr["entry_ts"] = pd.to_datetime(tr.entry_ts, utc=True, format="mixed")
    tr["exit_ts"] = pd.to_datetime(tr.exit_ts, utc=True, format="mixed")
    tr["qty"] = tr.lot_size * tr.n_lots

    rows = []
    skipped = {}
    for (date, name), g in tr.groupby(["date", "underlying"]):
        depth = load_day_mid(name, date, depth_root)
        if depth is None or depth.empty:
            skipped[(name, date)] = len(g)
            continue
        g = g.sort_values("entry_ts").reset_index(drop=True)
        # mid at entry, exit, and entry+h for each horizon
        def mid_for(target: pd.Series) -> np.ndarray:
            left = pd.DataFrame({"t": target.to_numpy(), "_i": np.arange(len(target))}).sort_values("t")
            m = pd.merge_asof(left, depth.rename(columns={"ts": "t"}), on="t",
                              direction="backward", tolerance=pd.Timedelta(seconds=5))
            return m.sort_values("_i")["mid"].to_numpy()

        mid_entry = mid_for(g.entry_ts)
        mid_exit = mid_for(g.exit_ts)
        per_h = {h: mid_for(g.entry_ts + pd.Timedelta(seconds=h)) for h in HORIZONS_S}

        for i, t in g.iterrows():
            r = {"date": date, "underlying": name, "direction": t.direction,
                 "qty": t.qty, "entry_price": t.entry_price, "exit_price": t.exit_price,
                 "sim_net": t.net_pnl, "fee": t.fee,
                 "mid_entry": mid_entry[i], "mid_exit": mid_exit[i]}
            for h in HORIZONS_S:
                r[f"mk_{h}"] = t.direction * (per_h[h][i] - t.entry_price)  # rupees/share
            rows.append(r)

    df = pd.DataFrame(rows)
    summary = _summarize(df) if not df.empty else {}
    summary["skipped_symbol_days"] = {f"{k[0]} {k[1]}": v for k, v in skipped.items()}
    return df, summary


def _summarize(df: pd.DataFrame) -> dict:
    qty = df.qty
    out = {"n_trades": len(df), "n_days": df.date.nunique()}
    # entry markout curve (rupees/share, and per-trade rupees ×qty)
    out["markout_per_share"] = {f"{h}s": round(float(df[f"mk_{h}"].mean()), 4) for h in HORIZONS_S}
    out["markout_rupees"] = {f"{h}s": round(float((df[f"mk_{h}"] * qty).mean()), 1) for h in HORIZONS_S}
    # captured edge = markout at h=0 (favorable fill vs mid)
    out["captured_edge_rs"] = out["markout_rupees"]["0s"]
    # mid-to-mid realistic PnL (both legs at mid, pure directional)
    midpnl = df.direction * (df.mid_exit - df.mid_entry) * qty - df.fee
    out["sim_net_total"] = round(float(df.sim_net.sum()), 0)
    out["mid_to_mid_total"] = round(float(midpnl.sum()), 0)
    out["spread_capture_component"] = round(out["sim_net_total"] - out["mid_to_mid_total"], 0)
    out["mid_to_mid_win_rate"] = round(float((midpnl > 0).mean()), 3)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--trades", required=True)
    p.add_argument("--depth-root", required=True)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    df, summary = compute(a.trades, a.depth_root)
    import json
    print(json.dumps(summary, indent=2, default=str))
    print("\n=== per-instrument mid-to-mid vs sim ===")
    if not df.empty:
        g = df.groupby("underlying").apply(
            lambda x: pd.Series({
                "trades": len(x),
                "sim_net": x.sim_net.sum(),
                "mid_to_mid": (x.direction * (x.mid_exit - x.mid_entry) * x.qty - x.fee).sum(),
                "mk_5s_rs": (x.mk_5 * x.qty).mean(),
                "mk_30s_rs": (x.mk_30 * x.qty).mean(),
            }), include_groups=False)
        print(g.round(0).to_string())
    if a.out:
        df.to_csv(a.out, index=False)
        print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
