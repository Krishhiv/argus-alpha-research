"""
Depth feed loader for NSE equity futures research.

Actual column names (verified from parquet files):
  Timestamp : collector_received_at  (UTC datetime64[ns, UTC])
  Prices    : bid_price_01..20, ask_price_01..20  (float64)
  Quantities: bid_qty_01..20, ask_qty_01..20      (int64)
  Orders    : bid_orders_01..20, ask_orders_01..20 (int64)

All timestamps are converted to IST (UTC+5:30) on load.
Session filter (9:20–15:25 IST) is applied by default.
"""

from __future__ import annotations

from pathlib import Path
import numpy as np
import pandas as pd

DATA_ROOT = Path(__file__).parent.parent.parent / "data" / "raw" / "depth"

LEVELS = 20
TICK_SIZE = 0.05
IST_OFFSET = pd.Timedelta("5h30min")

# Files with fewer session rows than this are treated as corrupt/incomplete.
# Healthy days have 88,000–101,000 session rows. 50,000 is a conservative floor.
MIN_SESSION_ROWS = 50_000

BID_PRICE_COLS  = [f"bid_price_{i:02d}" for i in range(1, LEVELS + 1)]
BID_QTY_COLS    = [f"bid_qty_{i:02d}"   for i in range(1, LEVELS + 1)]
BID_ORDER_COLS  = [f"bid_orders_{i:02d}" for i in range(1, LEVELS + 1)]
ASK_PRICE_COLS  = [f"ask_price_{i:02d}" for i in range(1, LEVELS + 1)]
ASK_QTY_COLS    = [f"ask_qty_{i:02d}"   for i in range(1, LEVELS + 1)]
ASK_ORDER_COLS  = [f"ask_orders_{i:02d}" for i in range(1, LEVELS + 1)]

ALL_DEPTH_COLS = (
    BID_PRICE_COLS + BID_QTY_COLS + BID_ORDER_COLS
    + ASK_PRICE_COLS + ASK_QTY_COLS + ASK_ORDER_COLS
)

META_COLS = [
    "collector_received_at", "underlying_symbol", "symbol_name",
    "security_id", "contract_key", "message_sequence",
    "bid_response_code", "ask_response_code",
]

SESSION_START = pd.Timedelta("9h20min")
SESSION_END   = pd.Timedelta("15h25min")


def _find_parquet(underlying: str, date: str) -> Path:
    """
    Find the compacted parquet file for a given underlying symbol and date.

    underlying : e.g. "HDFCBANK"
    date       : "YYYY-MM-DD"
    """
    date_dir = DATA_ROOT / f"trading_date={date}"
    if not date_dir.exists():
        raise FileNotFoundError(f"No data for date {date}: {date_dir}")

    matches = sorted(date_dir.glob(f"symbol={underlying}-*/compacted-*.parquet"))
    if not matches:
        raise FileNotFoundError(
            f"No parquet for {underlying} on {date} under {date_dir}"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple parquet files found for {underlying} on {date}: {matches}"
        )
    return matches[0]


def load_depth(
    underlying: str,
    date: str,
    session_filter: bool = True,
    path: Path | None = None,
) -> pd.DataFrame:
    """
    Load one day of depth data for a single instrument.

    Parameters
    ----------
    underlying     : Underlying symbol, e.g. "HDFCBANK"
    date           : "YYYY-MM-DD"
    session_filter : If True, keep only 9:20–15:25 IST rows (default True)
    path           : Override auto-discovery with an explicit file path

    Returns
    -------
    DataFrame sorted by ts_ist with derived columns:
        ts_ist      — IST timestamp (tz-naive for convenience)
        midprice    — (bid_price_01 + ask_price_01) / 2
        spread      — ask_price_01 - bid_price_01
        spread_ticks— spread / TICK_SIZE
        obi         — full-book order book imbalance
        obi_l1      — L1-only OBI
    """
    file = path if path is not None else _find_parquet(underlying, date)
    df = pd.read_parquet(file, columns=META_COLS + ALL_DEPTH_COLS)

    # Timestamp → IST tz-naive
    df["ts_ist"] = (
        df["collector_received_at"]
        .dt.tz_convert("UTC")
        .dt.tz_localize(None)
        + IST_OFFSET
    )
    df = df.sort_values("ts_ist").reset_index(drop=True)

    if session_filter:
        day = df["ts_ist"].dt.normalize()
        elapsed = df["ts_ist"] - day
        df = df[(elapsed >= SESSION_START) & (elapsed <= SESSION_END)].reset_index(drop=True)

        if len(df) < MIN_SESSION_ROWS:
            raise ValueError(
                f"Insufficient data for {underlying} {date}: "
                f"{len(df):,} session rows (minimum {MIN_SESSION_ROWS:,}). "
                f"File is likely corrupt or incomplete."
            )

    # Derived price columns
    df["midprice"]     = (df["bid_price_01"] + df["ask_price_01"]) / 2
    df["spread"]       = df["ask_price_01"] - df["bid_price_01"]
    df["spread_ticks"] = (df["spread"] / TICK_SIZE).round().astype(int)

    # Full-book OBI
    total_bid = df[BID_QTY_COLS].sum(axis=1)
    total_ask = df[ASK_QTY_COLS].sum(axis=1)
    df["obi"]    = (total_bid - total_ask) / (total_bid + total_ask + 1e-9)
    df["obi_l1"] = (
        (df["bid_qty_01"] - df["ask_qty_01"])
        / (df["bid_qty_01"] + df["ask_qty_01"] + 1e-9)
    )

    return df


def load_pair(
    underlying_a: str,
    underlying_b: str,
    date: str,
    session_filter: bool = True,
) -> pd.DataFrame:
    """
    Load two instruments for the same day, asof-joined on ts_ist.

    The returned DataFrame has columns suffixed _a and _b.
    Rows are anchored to underlying_a's timestamps.
    """
    df_a = load_depth(underlying_a, date, session_filter=session_filter)
    df_b = load_depth(underlying_b, date, session_filter=session_filter)

    df_a = df_a.rename(columns={c: f"{c}_a" for c in df_a.columns if c != "ts_ist"})
    df_b = df_b.rename(columns={c: f"{c}_b" for c in df_b.columns if c != "ts_ist"})

    merged = pd.merge_asof(
        df_a.sort_values("ts_ist"),
        df_b.sort_values("ts_ist"),
        on="ts_ist",
        direction="backward",
    )
    return merged


def safe_load_depth(
    underlying: str,
    date: str,
    session_filter: bool = True,
    path: Path | None = None,
) -> pd.DataFrame | None:
    """
    Wrapper around load_depth that returns None instead of raising on bad files.

    Handles:
      FileNotFoundError — parquet missing from disk (not yet synced, bad date)
      ValueError        — file exists but has too few session rows (corrupt/incomplete)

    Use this in any loop that iterates over many files so a single bad file
    does not crash the entire run. The caller should check for None and skip.
    """
    try:
        return load_depth(underlying, date, session_filter=session_filter, path=path)
    except FileNotFoundError:
        print(f"  SKIP {underlying} {date}: parquet file not found")
        return None
    except ValueError as e:
        print(f"  SKIP {underlying} {date}: {e}")
        return None


def extract_arrays(df: pd.DataFrame, suffix: str = "") -> dict[str, np.ndarray]:
    """
    Pull the 20-level price/qty/orders arrays out of a DataFrame into
    shape-(N, 20) numpy arrays for fast vectorised feature computation.

    suffix : "" for single instrument, "_a" or "_b" for pair DataFrames
    """
    s = suffix
    levels = range(1, LEVELS + 1)
    return {
        "bid_price":  df[[f"bid_price_{i:02d}{s}"  for i in levels]].to_numpy(float),
        "bid_qty":    df[[f"bid_qty_{i:02d}{s}"    for i in levels]].to_numpy(float),
        "bid_orders": df[[f"bid_orders_{i:02d}{s}" for i in levels]].to_numpy(float),
        "ask_price":  df[[f"ask_price_{i:02d}{s}"  for i in levels]].to_numpy(float),
        "ask_qty":    df[[f"ask_qty_{i:02d}{s}"    for i in levels]].to_numpy(float),
        "ask_orders": df[[f"ask_orders_{i:02d}{s}" for i in levels]].to_numpy(float),
    }
