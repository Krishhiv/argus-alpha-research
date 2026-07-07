"""
Realized-metrics computation for the paper-trader monitor.

Pure functions over the durable trade CSV - no HTTP, no global state - so they
are unit-testable and work whether or not the live trader is running.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from paper_trader.config import LOGS_DIR

IST = timezone(timedelta(hours=5, minutes=30))
ARMS_BASE = f"{LOGS_DIR}/arms"


def today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def read_trades(path: Path | str) -> list[dict]:
    """Read all trade rows from the CSV. Missing file → empty list."""
    p = Path(path)
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def _f(row: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(row.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def realized_metrics(rows: list[dict], date: str) -> dict[str, Any]:
    """Compute the day's realized trading statistics from trade rows."""
    day = [r for r in rows if r.get("date") == date]
    n = len(day)
    if n == 0:
        return {
            "n_trades": 0, "net_pnl": 0.0, "gross_pnl": 0.0, "fees": 0.0,
            "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "payoff": 0.0,
            "best": 0.0, "worst": 0.0,
            "per_instrument": {}, "exit_breakdown": {}, "equity_curve": [],
        }

    nets   = [_f(r, "net_pnl")   for r in day]
    gross  = [_f(r, "gross_pnl") for r in day]
    fees   = [_f(r, "fee")       for r in day]
    wins   = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]

    per_instrument: dict[str, dict] = {}
    for r in day:
        sym = r.get("underlying", "?")
        d = per_instrument.setdefault(sym, {"n": 0, "net": 0.0, "wins": 0})
        d["n"]   += 1
        d["net"] += _f(r, "net_pnl")
        d["wins"] += 1 if _f(r, "net_pnl") > 0 else 0
    for d in per_instrument.values():
        d["net"] = round(d["net"], 2)
        d["win_rate"] = round(d["wins"] / d["n"], 3) if d["n"] else 0.0

    exit_breakdown: dict[str, dict] = {}
    for r in day:
        m = r.get("exit_method", "?")
        d = exit_breakdown.setdefault(m, {"n": 0, "net": 0.0, "wins": 0})
        d["n"]   += 1
        d["net"] += _f(r, "net_pnl")
        d["wins"] += 1 if _f(r, "net_pnl") > 0 else 0
    for d in exit_breakdown.values():
        d["net"] = round(d["net"], 2)
        d["win_rate"] = round(d["wins"] / d["n"], 3) if d["n"] else 0.0

    ordered = sorted(day, key=lambda r: r.get("exit_ts", ""))
    cum = 0.0
    equity_curve = []
    for r in ordered:
        cum += _f(r, "net_pnl")
        equity_curve.append({"t": r.get("exit_ts", ""), "cum": round(cum, 2),
                             "pnl": round(_f(r, "net_pnl"), 2)})

    return {
        "n_trades":  n,
        "net_pnl":   round(sum(nets), 2),
        "gross_pnl": round(sum(gross), 2),
        "fees":      round(sum(fees), 2),
        "win_rate":  round(len(wins) / n, 3),
        "avg_win":   round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss":  round(sum(losses) / len(losses), 2) if losses else 0.0,
        "payoff":    round((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 2)
                     if wins and losses else 0.0,
        "best":      round(max(nets), 2),
        "worst":     round(min(nets), 2),
        "per_instrument": per_instrument,
        "exit_breakdown": exit_breakdown,
        "equity_curve":   equity_curve,
    }


# ── Multi-arm helpers ─────────────────────────────────────────────────────────

def discover_arms(arms_base: str | None = None) -> list[str]:
    """Names of arms that have a trade log under <arms_base>/<name>/."""
    base = Path(arms_base or ARMS_BASE)
    if not base.exists():
        return []
    return sorted(
        d.name for d in base.iterdir()
        if d.is_dir() and (d / "paper_trades.csv").exists()
    )


def arm_trades_path(name: str, arms_base: str | None = None) -> Path:
    return Path(arms_base or ARMS_BASE) / name / "paper_trades.csv"


def realized_for_arms(date: str, arms_base: str | None = None) -> dict[str, dict]:
    """{arm_name: realized_metrics(...)} for the given IST date."""
    return {
        name: realized_metrics(read_trades(arm_trades_path(name, arms_base)), date)
        for name in discover_arms(arms_base)
    }


def cumulative_for_arm(name: str, arms_base: str | None = None) -> dict[str, Any]:
    """All-time net per arm, broken down by trading date (for the daily email)."""
    rows = read_trades(arm_trades_path(name, arms_base))
    by_date: dict[str, float] = {}
    for r in rows:
        by_date[r.get("date", "")] = by_date.get(r.get("date", ""), 0.0) + _f(r, "net_pnl")
    return {
        "total_net": round(sum(by_date.values()), 2),
        "n_days":    len([d for d in by_date if d]),
        "n_trades":  len(rows),
        "by_date":   {d: round(v, 2) for d, v in sorted(by_date.items()) if d},
    }
