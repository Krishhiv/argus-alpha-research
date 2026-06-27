"""
Load stored depth parquet and derive the price series the lens runs on.

Reads the compacted depth (same files the collector stores / we synced to
/tmp/replay), keeps L1, applies the same garbage guard as the live broker
(drop bid/ask <= 0 or crossed books), and computes mid + microprice.

The Kalman lens runs on the MICROPRICE (a cleaner fair value than the mid).
dt is from collector_received_at (recv-side) — fine for a research prototype;
note that for production velocity you'd prefer an exchange timestamp.
"""

from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

L1_COLS = ["collector_received_at", "bid_price_01", "bid_qty_01",
           "ask_price_01", "ask_qty_01"]


def find_depth_file(name: str, date: str, data_dir: str = "/tmp/replay") -> str:
    paths = sorted(glob.glob(f"{data_dir}/{date}/{name}/*.parquet"))
    if not paths:
        raise FileNotFoundError(
            f"No depth parquet for {name} {date} under {data_dir} "
            f"(expected {data_dir}/{date}/{name}/*.parquet)")
    # prefer the compacted full-day file if present
    compacted = [p for p in paths if "compacted" in Path(p).name]
    return compacted[0] if compacted else paths[0]


def load_depth(name: str, date: str, data_dir: str = "/tmp/replay") -> pd.DataFrame:
    """Return a clean DataFrame with mid, microprice, micro_dev, dt (seconds)."""
    df = pd.read_parquet(find_depth_file(name, date, data_dir), columns=L1_COLS)
    df = df.sort_values("collector_received_at").reset_index(drop=True)

    bp, bq = df.bid_price_01.to_numpy(float), df.bid_qty_01.to_numpy(float)
    ap, aq = df.ask_price_01.to_numpy(float), df.ask_qty_01.to_numpy(float)

    # garbage guard — identical spirit to the live broker
    good = (bp > 0) & (ap > 0) & (ap >= bp)
    df = df.loc[good].reset_index(drop=True)
    bp, bq = bp[good], bq[good]
    ap, aq = ap[good], aq[good]

    tot = bq + aq
    df["mid"] = (bp + ap) / 2.0
    df["microprice"] = np.where(tot > 0, (bp * aq + ap * bq) / tot, df["mid"].to_numpy())
    df["micro_dev"] = df["microprice"] - df["mid"]

    ts = pd.to_datetime(df["collector_received_at"])
    df["dt"] = ts.diff().dt.total_seconds().fillna(0.0).clip(lower=0.0)
    return df
