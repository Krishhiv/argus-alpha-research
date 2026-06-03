"""
Live telemetry snapshot for the paper-trader monitor.

build_snapshot() assembles the current in-memory state of every PaperBroker
plus the shared DayRisk governor into one machine-readable dict.
write_snapshot_atomic() persists it (temp file + os.replace) so the monitor
server never reads a half-written file.

This captures only LIVE state (open positions, unrealized PnL, counters,
feed freshness, breaker). Realized trade history is durable in the CSV logs;
the monitor server reads those directly.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Optional


def build_snapshot(
    brokers: dict[str, Any],
    risk: Optional[Any] = None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Assemble a live telemetry snapshot from the running brokers + risk governor."""
    now = now or datetime.now(timezone.utc)
    bro = {sym: b.snapshot(now=now) for sym, b in brokers.items()}

    realized   = sum(s["realized_pnl"]   for s in bro.values())
    unrealized = sum(s["unrealized_pnl"] for s in bro.values())
    ages = [s["last_packet_age_sec"] for s in bro.values() if s["last_packet_age_sec"] is not None]

    return {
        "generated_at": now.isoformat(),
        "totals": {
            "realized_pnl":   round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl":      round(realized + unrealized, 2),
            "n_trades":       sum(s["n_trades"] for s in bro.values()),
            "n_posts":        sum(s["n_posts"]  for s in bro.values()),
            "n_fills":        sum(s["n_fills"]  for s in bro.values()),
            "open_positions": sum(1 for s in bro.values() if s["position_side"] != 0),
        },
        "risk": {
            "day_net_pnl": round(risk.day_net_pnl, 2) if risk is not None else None,
            "loss_limit":  risk.loss_limit            if risk is not None else None,
            "halted":      bool(risk.halted)          if risk is not None else False,
        },
        "feed": {
            "last_packet_age_sec": round(min(ages), 2) if ages else None,
        },
        "brokers": bro,
    }


def write_snapshot_atomic(path: Path, snapshot: dict[str, Any]) -> None:
    """Atomically persist a snapshot to JSON (temp file + os.replace)."""
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, sort_keys=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=".paper_telemetry.", delete=False,
    ) as tmp:
        tmp.write(payload)
        tmp.flush()
        temp_path = Path(tmp.name)
    os.replace(temp_path, path)


def load_snapshot(path: Path) -> dict[str, Any]:
    """Load one telemetry snapshot from JSON."""
    with Path(path).expanduser().resolve().open("r", encoding="utf-8") as fh:
        return json.load(fh)
