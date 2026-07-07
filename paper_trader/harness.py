"""
Multi-arm harness construction (pure - no network/feed dependency, so it is
unit-testable). main.py wires these into the live depth feed.

An ArmRuntime bundles one arm's independent state: its own DayRisk governor,
its own namespaced TradeLogger, and a PaperBroker per instrument in its universe.
The runner fans each depth packet out to every arm that trades that instrument.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from paper_trader.arms import ARMS, Arm
from paper_trader.broker import PaperBroker, DayRisk
from paper_trader.config import DAILY_LOSS_LIMIT, LOGS_DIR
from paper_trader.contracts import ResolvedContract, resolve_contracts
from paper_trader.logger import TradeLogger
from paper_trader.telemetry import build_snapshot

log = logging.getLogger("argus.harness")


@dataclass
class ArmRuntime:
    arm:     Arm
    risk:    DayRisk
    brokers: dict[str, PaperBroker]   # symbol -> broker (this arm's universe only)


def resolve_available(symbols: list[str]) -> dict[str, ResolvedContract]:
    """Resolve each symbol independently so one bad name can't crash the run."""
    contracts: dict[str, ResolvedContract] = {}
    for sym in symbols:
        try:
            contracts[sym] = resolve_contracts([sym])[sym]
        except (ValueError, KeyError) as exc:
            log.warning("Skipping %s - cannot resolve contract: %s", sym, exc)
    return contracts


def build_runtimes(contracts: dict[str, ResolvedContract],
                   arms: list[Arm] = ARMS,
                   loss_limit: float = DAILY_LOSS_LIMIT,
                   logs_dir: str | None = None) -> list[ArmRuntime]:
    """One ArmRuntime per arm; prune each arm's universe to resolvable symbols."""
    base = logs_dir or LOGS_DIR
    runtimes: list[ArmRuntime] = []
    for arm in arms:
        universe = [s for s in arm.universe if s in contracts]
        if not universe:
            log.warning("Arm %s has no resolvable instruments - skipping", arm.name)
            continue
        risk    = DayRisk(loss_limit)
        logger  = TradeLogger.for_dir(f"{base}/arms/{arm.name}")
        brokers = {
            sym: PaperBroker(sym, risk=risk, params=arm.params,
                             lot_size=contracts[sym].lot_size, logger=logger)
            for sym in universe
        }
        runtimes.append(ArmRuntime(arm, risk, brokers))
    return runtimes


def dispatch(runtimes: list[ArmRuntime], underlying: str, **packet) -> None:
    """Fan one depth packet out to every arm that trades this instrument."""
    for rt in runtimes:
        br = rt.brokers.get(underlying)
        if br is not None:
            br.on_depth_packet(**packet)


def build_combined(runtimes: list[ArmRuntime], *, now: datetime | None = None) -> dict:
    """Combined live telemetry across all arms (for the monitor)."""
    now = now or datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(),
        "arms": {
            rt.arm.name: {
                **build_snapshot(rt.brokers, rt.risk, now=now),
                "note":     rt.arm.note,
                "universe": list(rt.brokers.keys()),
            }
            for rt in runtimes
        },
    }
