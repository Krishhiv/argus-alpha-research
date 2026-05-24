"""
Walk-forward runner for the maker backtester.

Streams sessions one at a time, computes all features (including microprice,
ofi_ml, flow_composite), trims to the columns the maker engine needs, then
runs the MakerBacktester. Peak RAM is one trimmed session.

Typical usage
-------------
    from research.backtester.maker_walkforward import (
        run_maker_walkforward, maker_grid_search,
    )

    # Single param run
    result = run_maker_walkforward(
        entry_threshold=0.05,
        max_hold=200,
        order_timeout=20,
        exit_mode="maker",
    )
    print(result.summary())

    # Grid search
    results = maker_grid_search(param_grid={
        "entry_threshold": [0.03, 0.05, 0.10],
        "max_hold":        [50, 200, 500],
        "order_timeout":   [10, 20, 50],
        "exit_mode":       ["taker", "maker"],
    })
    print(results.head(10))
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Generator

import numpy as np
import pandas as pd

from research.data.load_data import load_depth
from research.features.depth_features import add_all_features
from research.backtester.maker_engine import (
    MakerBacktester, MakerCostModel, MakerTrade,
)
from research.backtester.walkforward import _build_inventory

DEFAULT_SIGNAL_COL = "micro_deviation"

# Columns the maker engine needs (signal column is appended at runtime).
_KEEP_COLS_BASE = ["ts_ist", "midprice", "bid_price_01", "ask_price_01"]

# Per-instrument NSE futures lot sizes (shares per contract).
# Inferred from median L1 bid_qty (assuming 2-lot quotes at L1):
#   HDFCBANK 1100 (550→1100 after Sep-2023 1:1 bonus, confirmed by price ~₹784)
#   ICICIBANK 700  (L1 median 1400 ÷ 2)
#   RELIANCE  500  (L1 median 1000 ÷ 2)
#   TCS       175  (L1 median  350 ÷ 2)
# VERIFY these against NSE's official lot-size circular before live trading.
LOT_SIZES: dict[str, float] = {
    "HDFCBANK":   550.0,
    "ICICIBANK":  700.0,
    "RELIANCE":   500.0,
    "TCS":        175.0,
}


# ---------------------------------------------------------------------------
# Session streamer
# ---------------------------------------------------------------------------

def _iter_maker_sessions(
    signal_col:  str              = DEFAULT_SIGNAL_COL,
    instruments: list[str] | None = None,
    dates:       list[str] | None = None,
    verbose:     bool             = True,
) -> Generator[dict, None, None]:
    """
    Yield one trimmed session at a time. Each yielded dict contains:
        df          : DataFrame with [ts_ist, midprice, bid_price_01, ask_price_01, signal_col]
        underlying  : str
        date        : str
    """
    inventory, prev_calib = _build_inventory()

    if instruments:
        inventory = inventory[inventory["underlying"].isin(instruments)]
    if dates:
        inventory = inventory[inventory["date"].isin(dates)]

    skipped = loaded = 0
    keep    = _KEEP_COLS_BASE + [signal_col]

    for _, row in inventory.iterrows():
        key     = (row["underlying"], row["date"])
        p_calib = prev_calib.get(key)
        if p_calib is None:
            skipped += 1
            continue

        try:
            df = load_depth(row["underlying"], row["date"],
                            session_filter=True, path=row["path"])
            df = add_all_features(df,
                                  size_threshold=p_calib,
                                  order_size=p_calib * 10)

            if signal_col not in df.columns:
                if verbose:
                    print(f"  SKIP {row['underlying']} {row['date']}: {signal_col} missing")
                continue

            df = df[keep].copy()

            loaded += 1
            if verbose:
                print(f"  {row['underlying']} {row['date']} ... {len(df):,} rows")

            yield {
                "df":         df,
                "underlying": row["underlying"],
                "date":       row["date"],
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
              f"({skipped} skipped — first day per instrument)")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class MakerWalkForwardResult:
    trades:    list[MakerTrade]
    params:    dict
    daily_pnl: pd.Series
    n_posts:   int
    n_fills:   int
    trade_log: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades)

    @property
    def total_fees(self) -> float:
        return sum(t.fee for t in self.trades)

    @property
    def fill_rate(self) -> float:
        return self.n_fills / self.n_posts if self.n_posts > 0 else float("nan")

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return float("nan")
        return sum(1 for t in self.trades if t.net_pnl > 0) / len(self.trades)

    @property
    def maker_exit_rate(self) -> float:
        if not self.trades:
            return float("nan")
        return sum(1 for t in self.trades if t.exit_method == "maker_exit") / len(self.trades)

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

    def exit_method_counts(self) -> dict:
        counts: dict = {}
        for t in self.trades:
            counts[t.exit_method] = counts.get(t.exit_method, 0) + 1
        return counts

    def save_trade_log(self, path: str) -> None:
        if self.trade_log.empty:
            print(f"  No trades to save → {path}")
            return
        self.trade_log.to_csv(path, index=False)
        print(f"  Trade log saved: {path} ({len(self.trade_log):,} rows)")

    def summary(self) -> dict:
        return {
            **self.params,
            "n_posts":         self.n_posts,
            "n_fills":         self.n_fills,
            "fill_rate":       round(self.fill_rate, 4),
            "n_trades":        self.n_trades,
            "maker_exit_rate": round(self.maker_exit_rate, 4) if self.trades else float("nan"),
            "net_pnl":         round(self.net_pnl, 2),
            "gross_pnl":       round(self.gross_pnl, 2),
            "total_fees":      round(self.total_fees, 2),
            "win_rate":        round(self.win_rate, 4) if self.trades else float("nan"),
            "profit_factor":   round(self.profit_factor, 4),
            "sharpe":          round(self.sharpe, 4),
            "max_drawdown":    round(self.max_drawdown, 2),
            "avg_hold":        round(self.avg_hold, 1) if self.trades else float("nan"),
        }


# ---------------------------------------------------------------------------
# Trade log builder
# ---------------------------------------------------------------------------

def _build_trade_log(tagged: list[dict]) -> pd.DataFrame:
    """
    Convert a list of {trade, underlying, date} dicts into a flat DataFrame.
    Each row is one completed round trip with all fields needed for analysis.
    """
    if not tagged:
        return pd.DataFrame()
    rows = []
    for item in tagged:
        t: MakerTrade = item["trade"]
        hold_pkts = t.exit_packet - t.entry_packet
        rows.append({
            "underlying":    item["underlying"],
            "date":          item["date"],
            "direction":     t.direction,
            "entry_ts":      t.entry_ts,
            "exit_ts":       t.exit_ts,
            "entry_price":   t.entry_price,
            "exit_price":    t.exit_price,
            "entry_method":  t.entry_method,
            "exit_method":   t.exit_method,
            "lot_size":      t.lot_size,
            "n_lots":        t.n_lots,
            "notional":      round(t.entry_price * t.lot_size * t.n_lots, 2),
            "hold_packets":  hold_pkts,
            "hold_secs":     round(hold_pkts * 0.4, 1),
            "gross_pnl":     t.gross_pnl,
            "fee":           t.fee,
            "net_pnl":       t.net_pnl,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Single walk-forward run
# ---------------------------------------------------------------------------

def run_maker_walkforward(
    entry_threshold: float,
    max_hold:        int,
    order_timeout:   int                       = 20,
    exit_mode:       str                       = "maker",
    cooldown:        int                       = 0,
    fresh_cross:     bool                      = False,
    signal_col:      str                       = DEFAULT_SIGNAL_COL,
    instruments:     list[str] | None          = None,
    dates:           list[str] | None          = None,
    lot_sizes:       dict[str, float] | None   = None,
    n_lots:          int                       = 1,
    cost_model:      MakerCostModel | None     = None,
    verbose:         bool                      = True,
) -> MakerWalkForwardResult:
    """
    Stream all sessions through the maker backtester with fixed parameters.
    Memory-safe — one session in RAM at a time.

    `lot_sizes` is a per-instrument dict, e.g. {"HDFCBANK": 1100, "TCS": 175}.
    Defaults to LOT_SIZES at the top of this file. Verify against NSE circulars.
    """
    _lot_sizes = lot_sizes if lot_sizes is not None else LOT_SIZES

    params = dict(
        signal_col      = signal_col,
        entry_threshold = entry_threshold,
        max_hold        = max_hold,
        order_timeout   = order_timeout,
        exit_mode       = exit_mode,
        cooldown        = cooldown,
        fresh_cross     = fresh_cross,
    )

    _bt_kwargs = dict(
        signal_col      = signal_col,
        entry_threshold = entry_threshold,
        max_hold        = max_hold,
        order_timeout   = order_timeout,
        exit_mode       = exit_mode,
        cooldown        = cooldown,
        fresh_cross     = fresh_cross,
        n_lots          = n_lots,
        cost_model      = cost_model,
    )

    all_trades:  list[MakerTrade] = []
    tagged:      list[dict]       = []
    daily_rows:  list[dict]       = []
    total_posts = 0
    total_fills = 0

    for s in _iter_maker_sessions(signal_col=signal_col, instruments=instruments,
                                  dates=dates, verbose=verbose):
        lot = _lot_sizes.get(s["underlying"], 550.0)
        bt  = MakerBacktester(lot_size=lot, **_bt_kwargs)
        result = bt.run(s["df"])
        all_trades.extend(result.trades)
        total_posts += result.n_posts
        total_fills += result.n_fills
        for t in result.trades:
            daily_rows.append({"date": t.exit_ts.date(), "net_pnl": t.net_pnl})
            tagged.append({"trade": t, "underlying": s["underlying"], "date": s["date"]})

    daily_pnl = (
        pd.DataFrame(daily_rows).groupby("date")["net_pnl"].sum()
        if daily_rows else pd.Series(dtype=float)
    )

    return MakerWalkForwardResult(
        trades=all_trades, params=params, daily_pnl=daily_pnl,
        n_posts=total_posts, n_fills=total_fills,
        trade_log=_build_trade_log(tagged),
    )


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------

def maker_grid_search(
    param_grid:  dict[str, list[Any]],
    signal_col:  str                        = DEFAULT_SIGNAL_COL,
    instruments: list[str] | None           = None,
    dates:       list[str] | None           = None,
    lot_sizes:   dict[str, float] | None    = None,
    n_lots:      int                        = 1,
    cost_model:  MakerCostModel | None      = None,
    verbose:     bool                       = True,
) -> pd.DataFrame:
    """
    Memory-safe grid search over maker parameters.

    `param_grid` may include any of:
        entry_threshold (required), max_hold (required),
        order_timeout, exit_mode, cooldown, fresh_cross.

    `lot_sizes` is a per-instrument dict; defaults to LOT_SIZES.

    Returns DataFrame sorted by Sharpe descending.
    """
    if "entry_threshold" not in param_grid or "max_hold" not in param_grid:
        raise ValueError("param_grid must include 'entry_threshold' and 'max_hold'")

    _lot_sizes = lot_sizes if lot_sizes is not None else LOT_SIZES

    keys     = list(param_grid.keys())
    combos   = list(itertools.product(*[param_grid[k] for k in keys]))
    n_combos = len(combos)

    def _make_bt(p: dict, lot: float) -> MakerBacktester:
        return MakerBacktester(
            signal_col      = signal_col,
            entry_threshold = p["entry_threshold"],
            max_hold        = p["max_hold"],
            order_timeout   = p.get("order_timeout", 20),
            exit_mode       = p.get("exit_mode", "maker"),
            cooldown        = p.get("cooldown", 0),
            fresh_cross     = p.get("fresh_cross", False),
            lot_size        = lot,
            n_lots          = n_lots,
            cost_model      = cost_model,
        )

    all_trades  = [[] for _ in combos]
    tagged_list = [[] for _ in combos]
    daily_rows  = [[] for _ in combos]
    total_posts = [0] * n_combos
    total_fills = [0] * n_combos

    n_sessions = 0
    for s in _iter_maker_sessions(signal_col=signal_col, instruments=instruments,
                                  dates=dates, verbose=verbose):
        n_sessions += 1
        lot = _lot_sizes.get(s["underlying"], 550.0)
        combos_params = [dict(zip(keys, c)) for c in combos]
        for i, p in enumerate(combos_params):
            bt = _make_bt(p, lot)
            result = bt.run(s["df"])
            all_trades[i].extend(result.trades)
            total_posts[i] += result.n_posts
            total_fills[i] += result.n_fills
            for t in result.trades:
                daily_rows[i].append({"date": t.exit_ts.date(), "net_pnl": t.net_pnl})
                tagged_list[i].append({"trade": t, "underlying": s["underlying"], "date": s["date"]})

    if verbose:
        print(f"\nMaker grid search: {n_combos} combos × {n_sessions} sessions\n")

    rows = []
    for i, combo in enumerate(combos):
        p = dict(zip(keys, combo))
        daily_pnl = (
            pd.DataFrame(daily_rows[i]).groupby("date")["net_pnl"].sum()
            if daily_rows[i] else pd.Series(dtype=float)
        )
        wf = MakerWalkForwardResult(
            trades=all_trades[i], params={**p, "signal_col": signal_col},
            daily_pnl=daily_pnl,
            n_posts=total_posts[i], n_fills=total_fills[i],
            trade_log=_build_trade_log(tagged_list[i]),
        )
        s = wf.summary()
        rows.append(s)

        if verbose:
            print(
                f"  thresh={p['entry_threshold']:.3f}  "
                f"hold={p['max_hold']:>4}  "
                f"to={p.get('order_timeout', 20):>3}  "
                f"em={p.get('exit_mode', 'maker'):>5}  "
                f"→  sharpe={s['sharpe']:>6.3f}  "
                f"net={s['net_pnl']:>+10.0f}  "
                f"trades={s['n_trades']:>5}  "
                f"fill_r={s['fill_rate']:.2f}  "
                f"mx_r={s['maker_exit_rate'] if not np.isnan(s['maker_exit_rate']) else 0:.2f}"
            )

    return (
        pd.DataFrame(rows)
        .sort_values("sharpe", ascending=False)
        .reset_index(drop=True)
    )
