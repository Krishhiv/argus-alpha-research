"""
Binary packet parsing for Dhan market and 20-level depth feeds.

Copied verbatim from the collector's packet_parser.py - kept as a local copy
so the paper trader has zero dependency on the collector's Python path.
Protocol source: Dhan WebSocket API binary format.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TypeAlias


MARKET_RESPONSE_LENGTHS = {
    2: 16,   # ticker
    4: 50,   # quote
    5: 12,   # oi
    6: 16,   # prev close
    50: 10,  # disconnect
}

DEPTH_RESPONSE_LENGTHS = {
    41: 332,  # bid side
    51: 332,  # ask side
    50: 14,   # disconnect
}

EXCHANGE_SEGMENT_MAP = {
    0: "IDX_I",
    1: "NSE_EQ",
    2: "NSE_FNO",
    3: "NSE_CURRENCY",
    4: "BSE_EQ",
    5: "MCX_COMM",
    7: "BSE_CURRENCY",
    8: "BSE_FNO",
}

_MARKET_HEADER  = struct.Struct("<B h B i")
_DEPTH_HEADER   = struct.Struct("<h B B i I")
_TICKER_PACKET  = struct.Struct("<f i")
_QUOTE_PACKET   = struct.Struct("<f h i f i i i f f f f")
_OI_PACKET      = struct.Struct("<i")
_PREV_CLOSE_PACKET  = struct.Struct("<f i")
_DISCONNECT_PACKET  = struct.Struct("<h")
_DEPTH_LEVEL    = struct.Struct("<d I I")


class PacketParseError(ValueError):
    """Raised when a binary packet cannot be parsed safely."""


@dataclass(frozen=True)
class MarketHeader:
    response_code: int
    message_length: int
    exchange_segment_code: int
    exchange_segment: str
    security_id: int


@dataclass(frozen=True)
class DepthHeader:
    message_length: int
    response_code: int
    exchange_segment_code: int
    exchange_segment: str
    security_id: int
    message_sequence: int


@dataclass(frozen=True)
class TickerPacket:
    header: MarketHeader
    ltp: float
    ltt_epoch_sec: int


@dataclass(frozen=True)
class QuotePacket:
    header: MarketHeader
    ltp: float
    ltq: int
    ltt_epoch_sec: int
    atp: float
    volume: int
    total_sell_qty: int
    total_buy_qty: int
    day_open: float
    day_close: float
    day_high: float
    day_low: float


@dataclass(frozen=True)
class OIPacket:
    header: MarketHeader
    open_interest: int


@dataclass(frozen=True)
class PrevClosePacket:
    header: MarketHeader
    prev_close: float
    prev_day_open_interest: int


@dataclass(frozen=True)
class DisconnectPacket:
    header: MarketHeader | DepthHeader
    disconnect_code: int


@dataclass(frozen=True)
class UnknownPacket:
    header: MarketHeader | DepthHeader
    payload: bytes


@dataclass(frozen=True)
class DepthLevel:
    price: float
    quantity: int
    order_count: int


@dataclass(frozen=True)
class DepthSidePacket:
    header: DepthHeader
    side: str          # 'bid' or 'ask'
    levels: tuple[DepthLevel, ...]


MarketPacket: TypeAlias = (
    TickerPacket | QuotePacket | OIPacket | PrevClosePacket | DisconnectPacket | UnknownPacket
)
DepthPacket: TypeAlias = DepthSidePacket | DisconnectPacket | UnknownPacket


def parse_market_feed_message(payload: bytes) -> list[MarketPacket]:
    """Parse one raw market-feed websocket message into typed packets."""
    packets: list[MarketPacket] = []
    offset = 0
    while offset < len(payload):
        packet, packet_length = _parse_market_packet(payload, offset)
        packets.append(packet)
        offset += packet_length
    return packets


def parse_depth_feed_message(payload: bytes) -> list[DepthPacket]:
    """Parse one raw 20-level depth websocket message into typed packets."""
    packets: list[DepthPacket] = []
    offset = 0
    while offset < len(payload):
        packet, packet_length = _parse_depth_packet(payload, offset)
        packets.append(packet)
        offset += packet_length
    return packets


def _parse_market_packet(payload: bytes, offset: int) -> tuple[MarketPacket, int]:
    if len(payload) - offset < _MARKET_HEADER.size:
        raise PacketParseError("Market packet shorter than 8-byte header")

    response_code, message_length, exchange_segment_code, security_id = (
        _MARKET_HEADER.unpack_from(payload, offset)
    )
    if message_length <= 0:
        raise PacketParseError(f"Market packet invalid message_length={message_length}")
    if len(payload) - offset < message_length:
        raise PacketParseError(
            f"Market packet truncated: needed {message_length}, got {len(payload) - offset}"
        )

    header = MarketHeader(
        response_code=response_code,
        message_length=message_length,
        exchange_segment_code=exchange_segment_code,
        exchange_segment=_exchange_segment_name(exchange_segment_code),
        security_id=security_id,
    )
    expected = MARKET_RESPONSE_LENGTHS.get(response_code)
    if expected is not None and message_length != expected:
        raise PacketParseError(
            f"Market packet code {response_code} expected {expected} bytes, got {message_length}"
        )

    body = offset + _MARKET_HEADER.size
    if response_code == 2:
        ltp, ltt = _TICKER_PACKET.unpack_from(payload, body)
        return TickerPacket(header=header, ltp=float(ltp), ltt_epoch_sec=ltt), message_length
    if response_code == 4:
        u = _QUOTE_PACKET.unpack_from(payload, body)
        return (
            QuotePacket(
                header=header, ltp=float(u[0]), ltq=int(u[1]), ltt_epoch_sec=int(u[2]),
                atp=float(u[3]), volume=int(u[4]), total_sell_qty=int(u[5]),
                total_buy_qty=int(u[6]), day_open=float(u[7]), day_close=float(u[8]),
                day_high=float(u[9]), day_low=float(u[10]),
            ),
            message_length,
        )
    if response_code == 5:
        (oi,) = _OI_PACKET.unpack_from(payload, body)
        return OIPacket(header=header, open_interest=oi), message_length
    if response_code == 6:
        pc, poi = _PREV_CLOSE_PACKET.unpack_from(payload, body)
        return PrevClosePacket(header=header, prev_close=float(pc), prev_day_open_interest=poi), message_length
    if response_code == 50:
        (dc,) = _DISCONNECT_PACKET.unpack_from(payload, body)
        return DisconnectPacket(header=header, disconnect_code=dc), message_length

    return UnknownPacket(header=header, payload=bytes(payload[body: offset + message_length])), message_length


def _parse_depth_packet(payload: bytes, offset: int) -> tuple[DepthPacket, int]:
    if len(payload) - offset < _DEPTH_HEADER.size:
        raise PacketParseError("Depth packet shorter than 12-byte header")

    message_length, response_code, exchange_segment_code, security_id, message_sequence = (
        _DEPTH_HEADER.unpack_from(payload, offset)
    )
    if message_length <= 0:
        raise PacketParseError(f"Depth packet invalid message_length={message_length}")
    if len(payload) - offset < message_length:
        raise PacketParseError(
            f"Depth packet truncated: needed {message_length}, got {len(payload) - offset}"
        )

    header = DepthHeader(
        message_length=message_length,
        response_code=response_code,
        exchange_segment_code=exchange_segment_code,
        exchange_segment=_exchange_segment_name(exchange_segment_code),
        security_id=security_id,
        message_sequence=message_sequence,
    )
    expected = DEPTH_RESPONSE_LENGTHS.get(response_code)
    if expected is not None and message_length != expected:
        raise PacketParseError(
            f"Depth packet code {response_code} expected {expected} bytes, got {message_length}"
        )

    body = offset + _DEPTH_HEADER.size
    if response_code in {41, 51}:
        levels = tuple(
            DepthLevel(
                price=float(price),
                quantity=int(qty),
                order_count=int(cnt),
            )
            for price, qty, cnt in (
                _DEPTH_LEVEL.unpack_from(payload, body + idx * _DEPTH_LEVEL.size)
                for idx in range(20)
            )
        )
        side = "bid" if response_code == 41 else "ask"
        return DepthSidePacket(header=header, side=side, levels=levels), message_length
    if response_code == 50:
        (dc,) = _DISCONNECT_PACKET.unpack_from(payload, body)
        return DisconnectPacket(header=header, disconnect_code=dc), message_length

    return UnknownPacket(header=header, payload=bytes(payload[body: offset + message_length])), message_length


def _exchange_segment_name(code: int) -> str:
    return EXCHANGE_SEGMENT_MAP.get(code, f"UNKNOWN_{code}")
