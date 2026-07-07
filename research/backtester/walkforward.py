"""
Walk-forward backtesting runner and parameter grid search.

Design - memory-safe streaming
-------------------------------
Sessions are processed one at a time: load parquet → compute features → trim to
3 columns (ts_ist, midprice, composite_eq) → run all parameter combos → discard.

Peak RAM is one session DataFrame (~2-3 MB) plus accumulated Trade objects.
The old "load everything first" approach used ~15-20 GB and crashed 8 GB machines.

Typical usage
-------------
    from research.backtester.walkforward import grid_search, run_walkforward

    # Sweep params - returns a DataFrame ranked by Sharpe
    results = grid_search(
        param_grid={
            "entry_threshold": [0.5, 1.0, 1.5, 2.0],
            "max_hold":        [10, 20, 50],
            "stop_ticks":      [3, 5, 8],
        },
    )
    print(results.head(10))

    # Single param run - returns a WalkForwardResult
    result = run_walkforward(entry_threshold=1.0, max_hold=20, stop_ticks=5)
    print(result.summary())
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any, Generator

import numpy as np
import pandas as pd

from research.data.load_data import DATA_ROOT, load_depth
from research.features.depth_features import add_all_features
from research.backtester.engine import Backtester, CostModel, Trade

SIGNAL_COL = "composite_eq"
_KEEP_COLS = ["ts_ist", "midprice", SIGNAL_COL]   # all the backtester needs


# ---------------------------------------------------------------------------
# Inventory + session generator
# ---------------------------------------------------------------------------

def _build_inventory() -> tuple[pd.DataFrame, dict]:
    """
    Discover all parquet files and build the previous-day calibration lookup.

    Returns
    -------
    inventory  : DataFrame[date, underlying, path]
    prev_calib : {(underlying, date) -> previous-day median(bid_qty_01)}
                 None for the first date per instrument (those sessions are skipped).
    """
    all_parquets = sorted(DATA_ROOT.glob("trading_date=*/symbol=*/compacted-*.parquet"))

    records = []
    for p in all_parquets:
        date_str    = p.parent.parent.name.split("=")[1]
        symbol_full = p.parent.name.split("=")[1]
        underlying  = symbol_full.split("-")[0]
        if underlying.startswith("BAJFINANCE"):
            continue
        records.append({"date": date_str, "underlying": underlying, "path": p})

    inventory = (
        pd.DataFrame(records)
        .sort_values(["underlying", "date"])
        .reset_index(drop=True)
    )

    calib: dict = {}
    for _, row in inventory.iterrows():
        try:
            df_c = pd.read_parquet(row["path"], columns=["bid_qty_01"])
            calib[(row["underlying"], row["date"])] = float(df_c["bid_qty_01"].median())
        except Exception:
            pass

    prev_calib: dict = {}
    for underlying in inventory["underlying"].unique():
        dates = sorted(inventory[inventory["underlying"] == underlying]["date"].unique())
        for i, date in enumerate(dates):
            prev_calib[(underlying, date)] = (
                calib.get((underlying, dates[i - 1])) if i > 0 else None
            )

    return inventory, prev_calib


def _iter_sessions(
    instruments: list[str] | None = None,
    dates:       list[str] | None = None,
    verbose:     bool             = True,
) -> Generator[dict, None, None]:
    """
    Yield one session dict at a time - never holds more than one DataFrame in RAM.

    Each dict has keys:
        df          : trimmed DataFrame with only [ts_ist, midprice, composite_eq]
        underlying  : str
        date        : str
        lot_size    : float (prev-day calibrated size_threshold)
        order_size  : float (prev-day calibrated order_size)
    """
    inventory, prev_calib = _build_inventory()

    if instruments:
        inventory = inventory[inventory["underlying"].isin(instruments)]
    if dates:
        inventory = inventory[inventory["date"].isin(dates)]

    skipped = 0
    loaded  = 0

    for _, row in inventory.iterrows():
        key     = (row["underlying"], row["date"])
        p_calib = prev_calib.get(key)

        if p_calib is None:
            skipped += 1
            continue

        size_threshold = p_calib
        order_size     = p_calib * 10

        try:
            df = load_depth(row["underlying"], row["date"],
                            session_filter=True, path=row["path"])
            df = add_all_features(df,
                                  size_threshold=size_threshold,
                                  order_size=order_size)

            if SIGNAL_COL not in df.columns:
                if verbose:
                    print(f"  SKIP {row['underlying']} {row['date']}: composite_eq missing")
                continue

            # Trim immediately - drops 140+ depth columns we no longer need
            df = df[_KEEP_COLS].copy()

            loaded += 1
            if verbose:
                print(f"  {row['underlying']} {row['date']} ... {len(df):,} rows"
                      f"  [thresh={size_threshold:.0f}]")

            yield {
                "df":         df,
                "underlying": row["underlying"],
                "date":       row["date"],
                "lot_size":   size_threshold,
                "order_size": order_size,
            }

        except FileNotFoundError:
            if verbose:
                print(f"  SKIP {row['underlying']} {row['date']}: file not found")
        except ValueError as e:
            if verbose:
                print(f"  SKIP {row['underlying']} {row['date']}: {e}")
        except Exception as e:
            if verbose:
                print(f"  ERROR {row['underlying']} {row['date']}: {e}")

    if verbose:
        print(f"\nStreamed {loaded} sessions "
              f"({skipped} skipped - first day per instrument)")


# ---------------------------------------------------------------------------
# WalkForwardResult
# ---------------------------------------------------------------------------

@dataclass
class WalkForwardResult:
    trades:    list[Trade]
    params:    dict
    daily_pnl: pd.Series    # net PnL summed by exit date

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return float("nan")
        return sum(1 for t in self.trades if t.net_pnl > 0) / len(self.trades)

    @property
    def profit_factor(self) -> float:
        wins   = sum(t.net_pnl for t in self.trades if t.net_pnl > 0)
        losses = abs(sum(t.net_pnl for t in self.trades if t.net_pnl <= 0))
        return wins / losses if losses > 0 else float("inf")

    @property
    def sharpe(self) -> float:
        if len(self.daily_pnl) < 2:
            return float("nan")
        return (self.daily_pnl.mean() / (self.daily_pnl.std() + 1e-9)) * np.sqrt(252)

    @property
    def max_drawdown(self) -> float:
        cum  = self.daily_pnl.cumsum()
        peak = cum.cummax()
        return float((cum - peak).min())

    @property
    def avg_hold(self) -> float:
        if not self.trades:
            return float("nan")
        return float(np.mean([t.exit_packet - t.entry_packet for t in self.trades]))

    def exit_reason_counts(self) -> dict:
        counts: dict = {}
        for t in self.trades:
            counts[t.exit_reason] = counts.get(t.exit_reason, 0) + 1
        return counts

    def summary(self) -> dict:
        return {
            **self.params,
            "n_trades":      self.n_trades,
            "net_pnl":       round(self.net_pnl, 2),
            "win_rate":      round(self.win_rate, 4)      if self.trades else float("nan"),
            "profit_factor": round(self.profit_factor, 4),
            "sharpe":        round(self.sharpe, 4),
            "max_drawdown":  round(self.max_drawdown, 2),
            "avg_hold":      round(self.avg_hold, 1)      if self.trades else float("nan"),
        }


# ---------------------------------------------------------------------------
# Single walk-forward run
# ---------------------------------------------------------------------------

def run_walkforward(
    entry_threshold: float,
    max_hold:        int,
    stop_ticks:      int,
    cooldown:        int              = 0,
    instruments:     list[str] | None = None,
    dates:           list[str] | None = None,
    lot_size:        float            = 550.0,
    n_lots:          int              = 1,
    cost_model:      CostModel | None = None,
    verbose:         bool             = True,
) -> WalkForwardResult:
    """
    Stream all sessions through the backtester with fixed parameters.
    Memory-safe - one session in RAM at a time.

    cooldown : minimum packets between exit and next entry (prevents churn on
               autocorrelated signals). 0 = no restriction.
    """
    params = dict(
        entry_threshold=entry_threshold,
        max_hold=max_hold,
        stop_ticks=stop_ticks,
        cooldown=cooldown,
    )

    bt = Backtester(
        signal_col      = SIGNAL_COL,
        entry_threshold = entry_threshold,
        max_hold        = max_hold,
        stop_ticks      = stop_ticks,
        cooldown        = cooldown,
        lot_size        = lot_size,
        n_lots          = n_lots,
        cost_model      = cost_model,
    )

    all_trades: list[Trade] = []
    daily_rows: list[dict]  = []

    for s in _iter_sessions(instruments=instruments, dates=dates, verbose=verbose):
        result = bt.run(s["df"])
        all_trades.extend(result.trades)
        for t in result.trades:
            daily_rows.append({"date": t.exit_ts.date(), "net_pnl": t.net_pnl})

    daily_pnl = (
        pd.DataFrame(daily_rows).groupby("date")["net_pnl"].sum()
        if daily_rows else pd.Series(dtype=float)
    )

    return WalkForwardResult(trades=all_trades, params=params, daily_pnl=daily_pnl)


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------

def grid_search(
    param_grid:  dict[str, list[Any]],
    instruments: list[str] | None    = None,
    dates:       list[str] | None    = None,
    lot_size:    float               = 550.0,
    n_lots:      int                 = 1,
    cost_model:  CostModel | None    = None,
    verbose:     bool                = True,
) -> pd.DataFrame:
    """
    Memory-safe grid search over entry_threshold × max_hold × stop_ticks × cooldown.

    Each session is loaded once and all parameter combos are evaluated on it
    before the DataFrame is discarded. Peak RAM ≈ one trimmed session (~3 MB)
    plus accumulated Trade objects.

    Returns a DataFrame of summary metrics sorted by Sharpe (descending).

    Example
    -------
    grid_search({
        "entry_threshold": [1.5, 2.0, 2.5, 3.0],
        "max_hold":        [50, 100, 200],
        "stop_ticks":      [5, 10, 15],
        "cooldown":        [20, 50],
    })
    """
    keys     = list(param_grid.keys())
    combos   = list(itertools.product(*[param_grid[k] for k in keys]))
    n_combos = len(combos)

    # Build one Backtester per combo - stateless across sessions, tiny objects
    backtesters = [
        Backtester(
            signal_col      = SIGNAL_COL,
            entry_threshold = dict(zip(keys, c))["entry_threshold"],
            max_hold        = dict(zip(keys, c))["max_hold"],
            stop_ticks      = dict(zip(keys, c))["stop_ticks"],
            cooldown        = dict(zip(keys, c)).get("cooldown", 0),
            fresh_cross     = dict(zip(keys, c)).get("fresh_cross", False),
            lot_size        = lot_size,
            n_lots          = n_lots,
            cost_model      = cost_model,
        )
        for c in combos
    ]

    # Per-combo accumulators - only Trade objects and (date, pnl) rows
    all_trades = [[] for _ in combos]
    daily_rows = [[] for _ in combos]

    n_sessions = 0
    for s in _iter_sessions(instruments=instruments, dates=dates, verbose=verbose):
        n_sessions += 1
        for i, bt in enumerate(backtesters):
            result = bt.run(s["df"])
            all_trades[i].extend(result.trades)
            for t in result.trades:
                daily_rows[i].append({"date": t.exit_ts.date(), "net_pnl": t.net_pnl})
        # s["df"] goes out of scope here; Python GC reclaims it

    if verbose:
        print(f"\nGrid search: {n_combos} combos × {n_sessions} sessions\n")

    rows = []
    for i, combo in enumerate(combos):
        p = dict(zip(keys, combo))
        daily_pnl = (
            pd.DataFrame(daily_rows[i]).groupby("date")["net_pnl"].sum()
            if daily_rows[i] else pd.Series(dtype=float)
        )
        wf = WalkForwardResult(trades=all_trades[i], params=p, daily_pnl=daily_pnl)
        s  = wf.summary()
        rows.append(s)

        if verbose:
            print(
                f"  thresh={p['entry_threshold']:.1f}  "
                f"hold={p['max_hold']:>3}  "
                f"stop={p['stop_ticks']}tk  "
                f"→  sharpe={s['sharpe']:>6.3f}  "
                f"wr={s['win_rate']:.3f}  "
                f"pf={s['profit_factor']:.3f}  "
                f"trades={s['n_trades']:>5}"
            )

    return (
        pd.DataFrame(rows)
        .sort_values("sharpe", ascending=False)
        .reset_index(drop=True)
    )
