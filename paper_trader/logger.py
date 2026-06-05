"""
CSV event logger for the paper trader.
Appends one row per event — no in-memory accumulation, survives crashes.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path

from paper_trader.config import TRADES_LOG, ORDERS_LOG, PNL_LOG

IST_OFFSET = 19800  # seconds

TRADE_FIELDS = [
    "underlying", "date", "direction",
    "entry_ts", "exit_ts",
    "entry_price", "exit_price",
    "entry_method", "exit_method",
    "fill_layer",        # 'depth_only' | 'depth+market' | 'queue_doubt'
    "lot_size", "n_lots", "notional",
    "hold_packets", "hold_secs",
    "gross_pnl", "fee", "net_pnl",
    "queue_ahead", "qty_consumed",
]

ORDER_FIELDS = [
    "ts", "underlying", "event",   # event: post | cancel | fill_candidate | fill_confirmed
    "side", "price", "qty",
    "fill_layer", "notes",
]

PNL_FIELDS = [
    "ts", "underlying", "date",
    "cumulative_net_pnl", "n_trades", "n_posts", "n_fills",
]


def _ensure(path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)


def _append(path: str, fields: list[str], row: dict) -> None:
    _ensure(path)
    write_header = not Path(path).exists() or Path(path).stat().st_size == 0
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


class TradeLogger:
    """
    Writes trades / orders / pnl CSVs to a given set of paths. Each parallel arm
    gets its own TradeLogger (its own directory), so arms never mix in one file.
    """

    def __init__(self, trades_path: str, orders_path: str, pnl_path: str) -> None:
        self.trades_path = trades_path
        self.orders_path = orders_path
        self.pnl_path    = pnl_path

    @classmethod
    def for_dir(cls, logs_dir: str) -> "TradeLogger":
        return cls(f"{logs_dir}/paper_trades.csv",
                   f"{logs_dir}/paper_orders.csv",
                   f"{logs_dir}/paper_pnl.csv")

    def trade(self, row: dict) -> None:
        _append(self.trades_path, TRADE_FIELDS, row)

    def order_event(self, underlying: str, event: str, side: int,
                    price: float, qty: float,
                    fill_layer: str = "", notes: str = "") -> None:
        _append(self.orders_path, ORDER_FIELDS, {
            "ts":         datetime.now(timezone.utc).isoformat(),
            "underlying": underlying, "event": event, "side": side,
            "price": price, "qty": qty, "fill_layer": fill_layer, "notes": notes,
        })

    def pnl_snapshot(self, underlying: str, date: str, cum_net_pnl: float,
                     n_trades: int, n_posts: int, n_fills: int) -> None:
        _append(self.pnl_path, PNL_FIELDS, {
            "ts": datetime.now(timezone.utc).isoformat(),
            "underlying": underlying, "date": date,
            "cumulative_net_pnl": round(cum_net_pnl, 2),
            "n_trades": n_trades, "n_posts": n_posts, "n_fills": n_fills,
        })


# Default logger → original top-level paths. Used by the single-arm path, tests,
# report.py and the monitor for backward compatibility.
default_logger = TradeLogger(TRADES_LOG, ORDERS_LOG, PNL_LOG)


def log_trade(row: dict) -> None:
    default_logger.trade(row)


def log_order_event(underlying: str, event: str, side: int, price: float,
                    qty: float, fill_layer: str = "", notes: str = "") -> None:
    default_logger.order_event(underlying, event, side, price, qty, fill_layer, notes)


def log_pnl_snapshot(underlying: str, date: str, cum_net_pnl: float,
                     n_trades: int, n_posts: int, n_fills: int) -> None:
    default_logger.pnl_snapshot(underlying, date, cum_net_pnl, n_trades, n_posts, n_fills)
