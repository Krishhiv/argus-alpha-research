"""
Data-prep for the A1 pairs/OU research - the VPS-side, headless equivalent of
01_data_prep.ipynb (no Jupyter needed; the collector venv runs this fine).

Run on the VPS:
    cd ~/paper-trader
    ~/collector-dhan/venv/bin/python research/pairs_ou/prep_panel.py

Writes research/pairs_ou/out/{panel.parquet, symbol_stats.csv}. Sync that `out/`
dir to your laptop and run 02_cointegration_screen.ipynb / 03_*.ipynb locally.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

import numpy as np
import pandas as pd

DATA_ROOT = os.path.expanduser("~/data/tbt-dhan/depth")
SYMBOLS   = ["HDFCBANK", "ICICIBANK", "RELIANCE", "SBIN", "AXISBANK", "BHARTIARTL", "ITC"]
BAR_FREQ  = "1min"
OUT_DIR   = Path(__file__).parent / "out"
LOT_SIZES = {"HDFCBANK": 550, "ICICIBANK": 700, "RELIANCE": 500,
             "SBIN": 750, "AXISBANK": 625, "BHARTIARTL": 475, "ITC": 1600}
L1 = ["collector_received_at", "bid_price_01", "ask_price_01"]


def _symbol_day_files(name: str, date_dir: str) -> list[str]:
    hits = sorted(glob.glob(f"{date_dir}/symbol={name}-*/compacted-*.parquet"))
    if not hits:
        hits = sorted(glob.glob(f"{date_dir}/symbol={name}-*/*.parquet"))
    return hits


def _load_symbol_day(name: str, date_dir: str) -> pd.DataFrame | None:
    files = _symbol_day_files(name, date_dir)
    if not files:
        return None
    comp = [p for p in files if "compacted" in Path(p).name]
    files = comp if comp else files
    df = pd.concat([pd.read_parquet(p, columns=L1) for p in files], ignore_index=True)
    bp, ap = df.bid_price_01.to_numpy(float), df.ask_price_01.to_numpy(float)
    good = (bp > 0) & (ap > 0) & (ap >= bp)
    df = df.loc[good]
    if df.empty:
        return None
    ts = pd.to_datetime(df.collector_received_at, utc=True)
    bp, ap = df.bid_price_01.to_numpy(float), df.ask_price_01.to_numpy(float)
    return pd.DataFrame({"ts": ts.to_numpy(), "mid": (bp + ap) / 2.0,
                         "spread": ap - bp}).sort_values("ts")


def _minute_bars(name: str):
    parts, spreads = [], []
    for date_dir in sorted(glob.glob(f"{DATA_ROOT}/trading_date=*")):
        d = _load_symbol_day(name, date_dir)
        if d is None:
            continue
        d = d.set_index("ts")
        parts.append(d["mid"].resample(BAR_FREQ).last().dropna())
        spreads.append(d["spread"].median())
    if not parts:
        return None, float("nan")
    ser = pd.concat(parts).sort_index()
    ser = ser[~ser.index.duplicated(keep="last")]
    return ser, float(np.nanmedian(spreads))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("depth root:", DATA_ROOT, "| exists:", os.path.isdir(DATA_ROOT))
    cols, stats = {}, []
    for s in SYMBOLS:
        ser, med_spread = _minute_bars(s)
        if ser is None or ser.empty:
            print(f"  {s:11} NO DATA")
            continue
        cols[s] = ser
        stats.append({"symbol": s, "n_bars": len(ser),
                      "first": ser.index.min(), "last": ser.index.max(),
                      "n_days": ser.index.normalize().nunique(),
                      "med_price": round(float(ser.median()), 2),
                      "med_spread": round(med_spread, 4), "lot": LOT_SIZES.get(s)})
        print(f"  {s:11} {len(ser):>6} bars  {ser.index.min().date()} → {ser.index.max().date()}")
    panel = pd.DataFrame(cols).sort_index()
    stats = pd.DataFrame(stats).set_index("symbol")
    panel.to_parquet(OUT_DIR / "panel.parquet")
    stats.to_csv(OUT_DIR / "symbol_stats.csv")
    print(f"\npanel shape: {panel.shape}")
    print(f"wrote {OUT_DIR/'panel.parquet'} and symbol_stats.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
