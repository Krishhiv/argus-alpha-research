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


def log_trade(row: dict) -> None:
    _append(TRADES_LOG, TRADE_FIELDS, row)


def log_order_event(underlying: str, event: str, side: int,
                    price: float, qty: float,
                    fill_layer: str = "", notes: str = "") -> None:
    _append(ORDERS_LOG, ORDER_FIELDS, {
        "ts":         datetime.now(timezone.utc).isoformat(),
        "underlying": underlying,
        "event":      event,
        "side":       side,
        "price":      price,
        "qty":        qty,
        "fill_layer": fill_layer,
        "notes":      notes,
    })


def log_pnl_snapshot(underlying: str, date: str,
                     cum_net_pnl: float, n_trades: int,
                     n_posts: int, n_fills: int) -> None:
    _append(PNL_LOG, PNL_FIELDS, {
        "ts":                datetime.now(timezone.utc).isoformat(),
        "underlying":        underlying,
        "date":              date,
        "cumulative_net_pnl": round(cum_net_pnl, 2),
        "n_trades":          n_trades,
        "n_posts":           n_posts,
        "n_fills":           n_fills,
    })
