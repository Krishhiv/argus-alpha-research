"""
Argus paper trader — entry point.

Two concurrent asyncio tasks run the Dhan depth and market feed websockets.
Depth packets drive per-instrument PaperBroker state machines.
Market packets update the LTP cache used for Layer 2 fill confirmation.
Shuts down cleanly on SIGTERM/SIGINT, force-closing any open position at mid.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

from paper_trader.broker import PaperBroker
from paper_trader.config import INSTRUMENTS
from paper_trader.contracts import resolve_security_ids
from paper_trader.feed_client import run_depth_feed, run_market_feed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("argus.paper_trader")

IST = timezone(timedelta(hours=5, minutes=30))


def _load_auth() -> tuple[str, str]:
    """Read DHAN_CLIENT_ID and access token from env + token file."""
    load_dotenv()
    client_id  = os.environ["DHAN_CLIENT_ID"]
    token_path = Path(os.environ["DHAN_ACCESS_TOKEN_PATH"]).expanduser()
    token      = token_path.read_text().strip()
    if not token:
        raise RuntimeError(f"Empty token file: {token_path}")
    return token, client_id


async def async_main() -> None:
    access_token, client_id = _load_auth()

    # Resolve current front-month security_ids from the collector's instrument
    # master CSV — automatically correct after each monthly expiry roll.
    security_ids = resolve_security_ids(INSTRUMENTS)
    log.info("Security IDs: %s", security_ids)

    brokers: dict[str, PaperBroker] = {inst: PaperBroker(inst) for inst in INSTRUMENTS}
    stop_event = asyncio.Event()

    def _shutdown(sig, frame) -> None:
        log.info("Signal %s received — force-closing open positions", sig)
        now = datetime.now(timezone.utc)
        for br in brokers.values():
            br.eod_force_close(ts_utc=now, mid=br.last_mid)
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    def on_depth(
        underlying: str,
        ts_utc: datetime,
        bid_price: float,
        bid_qty: int,
        ask_price: float,
        ask_qty: int,
    ) -> None:
        br = brokers.get(underlying)
        if br is not None:
            br.on_depth_packet(
                ts_utc=ts_utc,
                bid_price=bid_price,
                bid_qty=bid_qty,
                ask_price=ask_price,
                ask_qty=ask_qty,
            )

    def on_market(
        underlying: str,
        ltp: float,
        ltt_utc: datetime,
        recv_utc: datetime,
    ) -> None:
        br = brokers.get(underlying)
        if br is not None:
            br.on_market_packet(ltp=ltp, ltt_utc=ltt_utc, recv_utc=recv_utc)

    log.info("Starting Argus paper trader — instruments: %s", INSTRUMENTS)

    depth_task  = asyncio.create_task(
        run_depth_feed(access_token, client_id, on_depth, security_ids),
        name="depth-feed",
    )
    market_task = asyncio.create_task(
        run_market_feed(access_token, client_id, on_market, security_ids),
        name="market-feed",
    )

    await stop_event.wait()

    log.info("Stopping feed tasks")
    depth_task.cancel()
    market_task.cancel()
    await asyncio.gather(depth_task, market_task, return_exceptions=True)
    log.info("Paper trader stopped")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
