"""
Argus paper trader - multi-arm entry point.

One Dhan depth feed (subscribed to the union of all arms' instruments) is fanned
out to several strategy ARMS running in parallel as independent, risk-free
simulations (see paper_trader/arms.py). Each arm has its own brokers, DayRisk
governor, and namespaced CSV logs, so strategy variants compete head-to-head on
identical live data. Shuts down cleanly on SIGTERM/SIGINT, force-closing all
open positions in every arm. Harness construction lives in paper_trader/harness.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

from paper_trader.arms import all_universe_symbols, EXPENTURE_ARMS
from paper_trader.config import TELEMETRY_PATH, TELEMETRY_INTERVAL_SEC
from paper_trader.feed_client import run_depth_feed
from paper_trader.harness import resolve_available, build_runtimes, build_combined
from paper_trader.telemetry import write_snapshot_atomic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("argus.paper_trader")

IST = timezone(timedelta(hours=5, minutes=30))


def _load_auth() -> tuple[str, str]:
    load_dotenv()
    client_id  = os.environ["DHAN_CLIENT_ID"]
    token_path = Path(os.environ["DHAN_ACCESS_TOKEN_PATH"]).expanduser()
    token      = token_path.read_text().strip()
    if not token:
        raise RuntimeError(f"Empty token file: {token_path}")
    return token, client_id


async def async_main() -> None:
    access_token, client_id = _load_auth()

    arms      = EXPENTURE_ARMS               # Expenture I race (Recon I verdicts applied)
    symbols   = all_universe_symbols(arms)
    contracts = resolve_available(symbols)
    if not contracts:
        raise RuntimeError("No instruments resolved - cannot start.")
    security_ids = {s: c.security_id for s, c in contracts.items()}
    log.info("Universe (%d resolved): %s", len(security_ids), security_ids)

    runtimes = build_runtimes(contracts, arms)
    log.info("Running %d arms: %s", len(runtimes), [rt.arm.name for rt in runtimes])

    stop_event = asyncio.Event()

    def _shutdown(sig, frame) -> None:
        log.info("Signal %s received - force-closing all arms", sig)
        now = datetime.now(timezone.utc)
        for rt in runtimes:
            for br in rt.brokers.values():
                br.eod_force_close(ts_utc=now, mid=br.last_mid)
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    def on_depth(underlying, ts_utc, bid_price, bid_qty, ask_price, ask_qty) -> None:
        for rt in runtimes:
            br = rt.brokers.get(underlying)
            if br is not None:
                br.on_depth_packet(ts_utc=ts_utc, bid_price=bid_price, bid_qty=bid_qty,
                                   ask_price=ask_price, ask_qty=ask_qty)

    async def telemetry_loop() -> None:
        path = Path(TELEMETRY_PATH)
        while True:
            try:
                write_snapshot_atomic(path, build_combined(runtimes))
            except Exception as exc:
                log.warning("telemetry write failed: %s", exc)
            await asyncio.sleep(TELEMETRY_INTERVAL_SEC)

    depth_task     = asyncio.create_task(
        run_depth_feed(access_token, client_id, on_depth, security_ids), name="depth-feed")
    telemetry_task = asyncio.create_task(telemetry_loop(), name="telemetry")

    await stop_event.wait()

    log.info("Stopping feed tasks")
    depth_task.cancel()
    telemetry_task.cancel()
    await asyncio.gather(depth_task, telemetry_task, return_exceptions=True)
    try:
        write_snapshot_atomic(Path(TELEMETRY_PATH), build_combined(runtimes))
    except Exception as exc:
        log.warning("final telemetry write failed: %s", exc)
    log.info("Paper trader stopped")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
