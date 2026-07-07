"""
Lean AsyncIO websocket clients for Dhan depth and market feeds.

Each client runs in its own asyncio task and reconnects with exponential
backoff on any error. Callers pass synchronous callbacks; the event loop
calls them from the receive loop.

security_ids is a {underlying_symbol: security_id} dict resolved at startup
by paper_trader.contracts - automatically correct after monthly expiry rolls.

Bid+ask pairing for depth:
  Dhan sends bid (response_code=41) and ask (response_code=51) as separate
  binary packets, potentially in the same websocket frame or consecutive ones.
  We buffer the most recent side per security_id and fire the callback as
  soon as both sides are present for a given instrument.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets

from paper_trader.dhan_parser import (
    DepthSidePacket,
    DisconnectPacket,
    QuotePacket,
    TickerPacket,
    parse_depth_feed_message,
    parse_market_feed_message,
)

log = logging.getLogger("argus.feed")

_DEPTH_URL  = "wss://depth-api-feed.dhan.co/twentydepth"
_MARKET_URL = "wss://api-feed.dhan.co?version=2"

_BACKOFF_INIT = 5.0
_BACKOFF_MAX  = 60.0

DepthCallback  = Callable[[str, datetime, float, int, float, int], None]
MarketCallback = Callable[[str, float, datetime, datetime], None]


def _auth_url(base: str, token: str, client_id: str) -> str:
    parsed = urlsplit(base)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q.update({"token": token, "clientId": client_id, "authType": "2"})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(q), parsed.fragment))


def _subscribe(request_code: int, security_ids: dict[str, int]) -> str:
    instruments = [
        {"ExchangeSegment": "NSE_FNO", "SecurityId": str(sec_id)}
        for sec_id in security_ids.values()
    ]
    return json.dumps({
        "RequestCode": request_code,
        "InstrumentCount": len(instruments),
        "InstrumentList": instruments,
    })


async def run_depth_feed(
    access_token: str,
    client_id: str,
    on_depth: DepthCallback,
    security_ids: dict[str, int],
) -> None:
    """
    Runs forever (until cancelled). Connects to the 20-level depth feed,
    pairs bid+ask per instrument, and calls:
        on_depth(underlying, ts_utc, bid_price, bid_qty, ask_price, ask_qty)
    """
    url       = _auth_url(_DEPTH_URL, access_token, client_id)
    subscribe = _subscribe(23, security_ids)
    id_to_sym = {v: k for k, v in security_ids.items()}
    backoff   = _BACKOFF_INIT

    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                max_size=None,
            ) as ws:
                await ws.send(subscribe)
                log.info("Depth feed connected (%d instruments)", len(security_ids))
                backoff = _BACKOFF_INIT

                # pending[sec_id] = {"bid": pkt} or {"ask": pkt} or both
                pending: dict[int, dict[str, DepthSidePacket]] = {}

                async for raw in ws:
                    if not isinstance(raw, (bytes, bytearray, memoryview)):
                        continue
                    try:
                        packets = parse_depth_feed_message(bytes(raw))
                    except Exception as exc:
                        log.warning("Depth parse error: %s", exc)
                        continue

                    ts = datetime.now(timezone.utc)

                    for pkt in packets:
                        if isinstance(pkt, DepthSidePacket):
                            sec_id = pkt.header.security_id
                            sym    = id_to_sym.get(sec_id)
                            if sym is None:
                                continue
                            sides = pending.setdefault(sec_id, {})
                            sides[pkt.side] = pkt
                            if "bid" in sides and "ask" in sides:
                                bid = sides.pop("bid")
                                ask = sides.pop("ask")
                                on_depth(
                                    sym, ts,
                                    bid.levels[0].price, bid.levels[0].quantity,
                                    ask.levels[0].price, ask.levels[0].quantity,
                                )
                        elif isinstance(pkt, DisconnectPacket):
                            log.warning("Depth feed disconnect packet: code=%s", pkt.disconnect_code)

        except asyncio.CancelledError:
            log.info("Depth feed task cancelled")
            return
        except Exception as exc:
            log.warning("Depth feed error: %s - retrying in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(_BACKOFF_MAX, backoff * 2)


async def run_market_feed(
    access_token: str,
    client_id: str,
    on_market: MarketCallback,
    security_ids: dict[str, int],
) -> None:
    """
    Runs forever (until cancelled). Connects to the market (LTP) feed and calls:
        on_market(underlying, ltp, ltt_utc, recv_utc)

    ltt_utc is corrected from Dhan's IST-epoch ltt_epoch_sec by subtracting 19800s
    (the +05:30 IST offset), giving a true UTC timestamp.
    """
    url       = _auth_url(_MARKET_URL, access_token, client_id)
    subscribe = _subscribe(17, security_ids)
    id_to_sym = {v: k for k, v in security_ids.items()}
    backoff   = _BACKOFF_INIT

    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                max_size=None,
            ) as ws:
                await ws.send(subscribe)
                log.info("Market feed connected (%d instruments)", len(security_ids))
                backoff = _BACKOFF_INIT

                async for raw in ws:
                    if not isinstance(raw, (bytes, bytearray, memoryview)):
                        continue
                    try:
                        packets = parse_market_feed_message(bytes(raw))
                    except Exception as exc:
                        log.warning("Market parse error: %s", exc)
                        continue

                    recv = datetime.now(timezone.utc)
                    for pkt in packets:
                        if isinstance(pkt, (TickerPacket, QuotePacket)):
                            sec_id = pkt.header.security_id
                            sym    = id_to_sym.get(sec_id)
                            if sym is None:
                                continue
                            # ltt_epoch_sec is stored as IST epoch (not UTC); subtract IST offset
                            ltt = datetime.utcfromtimestamp(
                                pkt.ltt_epoch_sec - 19_800
                            ).replace(tzinfo=timezone.utc)
                            on_market(sym, pkt.ltp, ltt, recv)
                        elif isinstance(pkt, DisconnectPacket):
                            log.warning("Market feed disconnect packet: code=%s", pkt.disconnect_code)

        except asyncio.CancelledError:
            log.info("Market feed task cancelled")
            return
        except Exception as exc:
            log.warning("Market feed error: %s - retrying in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(_BACKOFF_MAX, backoff * 2)
