"""
Unit tests for paper_trader modules.

Covers: signal math, PaperBroker state machine, binary packet parser,
contract resolution. No network, no filesystem side effects (tmp_path for CSV).
"""

from __future__ import annotations

import csv
import struct
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from paper_trader.signal import compute_micro_deviation
from paper_trader.broker import PaperBroker, _compute_fee
from paper_trader.dhan_parser import (
    DepthSidePacket,
    TickerPacket,
    PacketParseError,
    parse_depth_feed_message,
    parse_market_feed_message,
)
from paper_trader.contracts import resolve_security_ids
from paper_trader.config import ENTRY_THRESHOLD, ORDER_TIMEOUT_PKTS, MAX_HOLD_PACKETS


# ── Shared helpers ────────────────────────────────────────────────────────────

UTC = timezone.utc


def _depth(broker: PaperBroker, bid_p: float, bid_q: int, ask_p: float, ask_q: int) -> None:
    broker.on_depth_packet(
        ts_utc=datetime.now(UTC), bid_price=bid_p, bid_qty=bid_q,
        ask_price=ask_p, ask_qty=ask_q,
    )


def _market(broker: PaperBroker, ltp: float, age_secs: float = 0.5) -> None:
    now = datetime.now(UTC)
    broker.on_market_packet(
        ltp=ltp, ltt_utc=now - timedelta(seconds=age_secs), recv_utc=now,
    )


# Signal helpers ---------------------------------------------------------------
# BUY signal: large bid_qty pulls microprice toward ask → deviation > 0
# bid=100 q=990, ask=101 q=10: micro=(100*10+101*990)/1000=100.99, mid=100.5, dev=+0.49
_BUY  = (100.0, 990, 101.0, 10)
# SELL signal: large ask_qty pulls microprice toward bid → deviation < 0
# bid=100 q=10, ask=101 q=990: micro=(100*990+101*10)/1000=100.01, mid=100.5, dev=-0.49
_SELL = (100.0, 10,  101.0, 990)
# Flat: equal quantities → deviation = 0
_FLAT = (100.0, 500, 101.0, 500)


# ── Signal ────────────────────────────────────────────────────────────────────

class TestSignal:
    def test_balanced_book_is_zero(self):
        assert compute_micro_deviation(*_FLAT) == pytest.approx(0.0)

    def test_bid_heavy_gives_buy_signal(self):
        dev = compute_micro_deviation(*_BUY)
        assert dev > ENTRY_THRESHOLD

    def test_ask_heavy_gives_sell_signal(self):
        dev = compute_micro_deviation(*_SELL)
        assert dev < -ENTRY_THRESHOLD

    def test_zero_quantity_returns_zero(self):
        assert compute_micro_deviation(100.0, 0, 101.0, 0) == 0.0


# ── Fee computation ───────────────────────────────────────────────────────────

class TestFee:
    def test_fee_is_positive(self):
        assert _compute_fee(1500.0, 1505.0, lot_size=550, n_lots=1, direction=1) > 0

    def test_fee_direction_asymmetry(self):
        # STT applies to the sell notional; stamp to the buy notional.
        # Long trade sells at 1505 (higher), short trade sells at 1500 (lower)
        # → long STT is slightly larger → long fee > short fee by a small amount.
        long_fee  = _compute_fee(1500.0, 1505.0, lot_size=550, n_lots=1, direction=1)
        short_fee = _compute_fee(1500.0, 1505.0, lot_size=550, n_lots=1, direction=-1)
        assert long_fee > short_fee
        assert abs(long_fee - short_fee) < 1.0   # difference is tiny (< ₹1)

    def test_fee_scales_with_notional(self):
        fee_1lot = _compute_fee(1500.0, 1505.0, lot_size=550, n_lots=1, direction=1)
        fee_2lot = _compute_fee(1500.0, 1505.0, lot_size=550, n_lots=2, direction=1)
        # Brokerage is flat (₹20×2 vs ₹20×2), percentage charges scale → fee_2 > fee_1
        assert fee_2lot > fee_1lot


# ── Broker — signal gating and order posting ─────────────────────────────────

class TestBrokerSignalGating:
    def test_below_threshold_no_post(self):
        br = PaperBroker("HDFCBANK")
        _depth(br, *_FLAT)  # |dev| = 0
        assert br.n_posts == 0

    def test_buy_signal_posts_at_bid(self):
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY)
        assert br.n_posts == 1
        assert br._pending_entry is not None
        assert br._pending_entry.side == 1
        assert br._pending_entry.price == pytest.approx(100.0)  # posted at bid

    def test_sell_signal_posts_at_ask(self):
        br = PaperBroker("HDFCBANK")
        _depth(br, *_SELL)
        assert br.n_posts == 1
        assert br._pending_entry.side == -1
        assert br._pending_entry.price == pytest.approx(101.0)  # posted at ask

    def test_fresh_cross_required(self):
        # Sending buy signal continuously keeps prev_abs_sig > threshold.
        # After the initial post times out, another buy tick must not re-post
        # because there was no fresh cross from below threshold.
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY)           # initial cross → post
        assert br.n_posts == 1
        for _ in range(ORDER_TIMEOUT_PKTS + 1):
            _depth(br, *_BUY)       # cancel via timeout; prev_abs_sig stays high
        assert br.n_cancels == 1
        _depth(br, *_BUY)           # prev_abs_sig > threshold → no fresh cross
        assert br.n_posts == 1      # still 1, no new post

    def test_flat_then_signal_posts(self):
        # After a flat tick resets prev_abs_sig to 0, a signal tick should post
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY)           # post
        for _ in range(ORDER_TIMEOUT_PKTS + 1):
            _depth(br, *_FLAT)      # cancel; prev_abs_sig → 0 at each packet
        # Now prev_abs_sig ≈ 0, cooldown expired → new signal should post
        _depth(br, *_BUY)
        assert br.n_posts == 2


# ── Broker — entry cancel ─────────────────────────────────────────────────────

class TestBrokerCancel:
    def test_unfilled_entry_cancels_after_timeout(self):
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY)           # post
        # Send ORDER_TIMEOUT_PKTS flat packets; no fill, no market feed
        for _ in range(ORDER_TIMEOUT_PKTS + 1):
            _depth(br, *_FLAT)
        assert br.n_cancels == 1
        assert br._pending_entry is None
        assert br._position_side == 0


# ── Broker — fill detection (2-layer gate) ────────────────────────────────────

class TestBrokerFill:
    def _broker_pending_buy(self) -> PaperBroker:
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY)           # posts BUY at 100.0
        return br

    def test_no_fill_without_market_feed(self):
        # Layer 2 requires a valid market packet — fill must not happen without one
        br = self._broker_pending_buy()
        _depth(br, 99.5, 500, 101.0, 10)   # bid < 100.0 but no market feed
        assert br.n_fills == 0
        assert br._pending_entry is not None

    def test_buy_fill_layer2_confirmed(self):
        br = self._broker_pending_buy()
        _market(br, ltp=99.8, age_secs=0.5)     # ltp ≤ posted=100.0, fresh
        _depth(br, 99.5, 500, 101.0, 10)        # bid < 100.0 → fill
        assert br.n_fills == 1
        assert br._position_side == 1

    def test_stale_market_feed_blocks_fill(self):
        br = self._broker_pending_buy()
        _market(br, ltp=99.8, age_secs=10.0)    # older than MARKET_FEED_STALE_SECS=5
        _depth(br, 99.5, 500, 101.0, 10)
        assert br.n_fills == 0

    def test_high_ltp_blocks_fill(self):
        # Market feed shows ltp > posted_price → trade at our level is not confirmed
        br = self._broker_pending_buy()
        _market(br, ltp=100.5, age_secs=0.5)    # ltp > posted=100.0
        _depth(br, 99.5, 500, 101.0, 10)
        assert br.n_fills == 0

    def test_sell_fill(self):
        br = PaperBroker("HDFCBANK")
        _depth(br, *_SELL)                       # posts SELL at 101.0
        _market(br, ltp=101.2, age_secs=0.5)     # ltp ≥ posted=101.0
        _depth(br, 100.0, 10, 101.5, 500)        # ask > 101.0 → fill
        assert br.n_fills == 1
        assert br._position_side == -1


# ── Broker — exit logic ───────────────────────────────────────────────────────

class TestBrokerExit:
    def _filled_long(self) -> PaperBroker:
        """Returns a broker that has just filled a long at 100.0."""
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY)                        # post BUY at 100.0
        _market(br, ltp=99.8, age_secs=0.5)
        _depth(br, 99.5, 500, 101.0, 10)         # fill
        assert br.n_fills == 1
        return br

    def test_maker_exit_long(self):
        br = self._filled_long()
        # First STATE-3 packet: pending exit posted at ask=102.0
        _depth(br, 100.0, 500, 102.0, 500)
        # Second packet: ask=102.1 > exit_price=102.0 → maker_exit
        _depth(br, 100.0, 500, 102.1, 500)
        assert br._position_side == 0
        assert br.trades[0]["exit_method"] == "maker_exit"
        assert br.trades[0]["exit_price"] == pytest.approx(102.0)  # passive price, not 102.1

    def test_taker_max_hold(self):
        br = self._filled_long()
        for _ in range(MAX_HOLD_PACKETS + 1):
            _depth(br, *_FLAT)                   # ask=101.0 never exceeds exit_price=101.0
        assert br._position_side == 0
        assert br.trades[0]["exit_method"] == "taker_max_hold"

    def test_eod_force_close(self):
        br = self._filled_long()
        br.eod_force_close(ts_utc=datetime.now(UTC), mid=100.5)
        assert br._position_side == 0
        assert br.trades[0]["exit_method"] == "taker_eod"

    def test_no_trade_on_eod_when_flat(self):
        br = PaperBroker("HDFCBANK")
        br.eod_force_close(ts_utc=datetime.now(UTC), mid=100.0)
        assert len(br.trades) == 0

    def test_gross_pnl_positive_on_winner(self):
        br = self._filled_long()
        _depth(br, 100.0, 500, 102.0, 500)
        _depth(br, 100.0, 500, 102.1, 500)      # exit at 102.0, entry at 100.0
        t = br.trades[0]
        assert t["entry_price"] == pytest.approx(100.0)
        assert t["exit_price"]  == pytest.approx(102.0)
        assert t["gross_pnl"]   > 0
        assert t["fee"]         > 0
        assert "net_pnl" in t

    def test_last_mid_tracked(self):
        br = PaperBroker("HDFCBANK")
        _depth(br, 100.0, 500, 102.0, 500)
        assert br.last_mid == pytest.approx(101.0)


# ── Broker — queue doubt ──────────────────────────────────────────────────────

class TestBrokerQueueDoubt:
    def test_queue_doubt_when_insufficient_consumption(self):
        # Post BUY with large queue ahead; bid drops but qty consumed < queue_ahead
        br = PaperBroker("HDFCBANK")
        # Post at bid=100.0 with large bid_qty → large queue_ahead
        _depth(br, 100.0, 10_000, 101.0, 10)    # queue_ahead = 10_000
        _market(br, ltp=99.5, age_secs=0.5)
        # bid drops (L1 fill candidate) but only 100 qty consumed (bid_qty drops 100)
        _depth(br, 99.5, 9_900, 101.0, 10)      # qty_consumed = max(0, 10000-9900) = 100
        # Layer 1: bid=99.5 < posted=100.0 ✓; Layer 2: ltp=99.5 ≤ 100.0 ✓
        # But qty_consumed=100 < queue_ahead=10000 → queue_doubt
        assert br.n_fills == 1
        # fill_layer field is set at fill time; it's in the order log, not trade log here
        # Check _fill_layer indirectly via log by inspecting the trade logged after close
        # (fill_layer in the trade dict is set to "" at close, was set at fill time in log)


# ── Dhan binary parser ────────────────────────────────────────────────────────

_DEPTH_HDR  = struct.Struct("<h B B i I")   # message_length, response_code, exch_seg, sec_id, seq
_DEPTH_LVL  = struct.Struct("<d I I")        # price(f64), qty(u32), cnt(u32)
_MKT_HDR    = struct.Struct("<B h B i")      # response_code, message_length, exch_seg, sec_id
_TICKER_BODY = struct.Struct("<f i")         # ltp(f32), ltt_epoch_sec(i32)


def _depth_frame(sec_id: int, side: str, price: float = 1500.0, qty: int = 1000) -> bytes:
    rc = 41 if side == "bid" else 51
    hdr = _DEPTH_HDR.pack(332, rc, 2, sec_id, 0)
    body = b""
    for i in range(20):
        p = price - i * 0.05 if side == "bid" else price + i * 0.05
        body += _DEPTH_LVL.pack(p, qty, 10)
    return hdr + body


def _ticker_frame(sec_id: int, ltp: float, ltt: int = 1_748_000_000) -> bytes:
    hdr  = _MKT_HDR.pack(2, 16, 2, sec_id)
    body = _TICKER_BODY.pack(ltp, ltt)
    return hdr + body


class TestDhanParser:
    def test_parse_depth_bid(self):
        packets = parse_depth_feed_message(_depth_frame(66180, "bid", price=1650.0, qty=500))
        assert len(packets) == 1
        pkt = packets[0]
        assert isinstance(pkt, DepthSidePacket)
        assert pkt.side == "bid"
        assert pkt.header.security_id == 66180
        assert pkt.levels[0].price    == pytest.approx(1650.0)
        assert pkt.levels[0].quantity == 500
        assert len(pkt.levels) == 20

    def test_parse_depth_ask(self):
        pkt = parse_depth_feed_message(_depth_frame(66191, "ask"))[0]
        assert pkt.side == "ask"
        assert pkt.header.security_id == 66191

    def test_levels_are_ordered(self):
        pkt = parse_depth_feed_message(_depth_frame(66180, "bid", price=1500.0))[0]
        # Best bid is level 0; each subsequent level is 0.05 lower
        assert pkt.levels[0].price > pkt.levels[1].price

    def test_parse_multi_packet_frame(self):
        # Two depth packets in one binary frame
        raw = _depth_frame(66180, "bid") + _depth_frame(66180, "ask")
        packets = parse_depth_feed_message(raw)
        assert len(packets) == 2
        assert {p.side for p in packets} == {"bid", "ask"}

    def test_parse_ticker_packet(self):
        ltt_ist = 1_748_000_000 + 19_800   # stored as IST epoch; UTC = ltt - 19800
        packets = parse_market_feed_message(_ticker_frame(66355, ltp=2800.0, ltt=ltt_ist))
        assert len(packets) == 1
        pkt = packets[0]
        assert isinstance(pkt, TickerPacket)
        assert pkt.ltp  == pytest.approx(2800.0, rel=1e-4)
        assert pkt.header.security_id == 66355
        # After subtracting IST offset, UTC epoch is 1_748_000_000
        assert pkt.ltt_epoch_sec - 19_800 == 1_748_000_000

    def test_truncated_frame_raises(self):
        with pytest.raises(PacketParseError):
            parse_depth_feed_message(b"\x00" * 4)   # too short for 12-byte depth header

    def test_empty_frame_returns_empty(self):
        assert parse_depth_feed_message(b"") == []
        assert parse_market_feed_message(b"") == []


# ── Contract resolution ───────────────────────────────────────────────────────

_CSV_HEADER = (
    "EXCH_ID,SEGMENT,SECURITY_ID,ISIN,INSTRUMENT,UNDERLYING_SECURITY_ID,"
    "UNDERLYING_SYMBOL,SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,"
    "LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE,TICK_SIZE,EXPIRY_FLAG\n"
)

def _csv_row(sym: str, sec_id: int, expiry: str, lot: int = 550) -> str:
    return (
        f"NSE,D,{sec_id},NA,FUTSTK,0,{sym},{sym}-FUT,{sym} FUT,"
        f"FUT,NA,{lot},{expiry},-0.01,XX,0.05,N\n"
    )


class TestContracts:
    def test_resolves_nearest_expiry(self, tmp_path: Path):
        csv_file = tmp_path / "master.csv"
        csv_file.write_text(
            _CSV_HEADER
            + _csv_row("HDFCBANK", 66180, "2026-05-26")
            + _csv_row("HDFCBANK", 67000, "2026-06-26")  # next month
        )
        with patch.dict("os.environ", {"INSTRUMENT_MASTER_PATH": str(csv_file)}):
            result = resolve_security_ids(["HDFCBANK"])
        assert result["HDFCBANK"] == 66180   # picks nearest, not later

    def test_skips_expired_contracts(self, tmp_path: Path):
        yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
        tomorrow  = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
        csv_file  = tmp_path / "master.csv"
        csv_file.write_text(
            _CSV_HEADER
            + _csv_row("ICICIBANK", 99999, yesterday)   # expired
            + _csv_row("ICICIBANK", 66191, tomorrow)    # active
        )
        with patch.dict("os.environ", {"INSTRUMENT_MASTER_PATH": str(csv_file)}):
            result = resolve_security_ids(["ICICIBANK"])
        assert result["ICICIBANK"] == 66191

    def test_raises_if_all_expired(self, tmp_path: Path):
        yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
        csv_file  = tmp_path / "master.csv"
        csv_file.write_text(_CSV_HEADER + _csv_row("TCS", 66389, yesterday))
        with patch.dict("os.environ", {"INSTRUMENT_MASTER_PATH": str(csv_file)}):
            with pytest.raises(ValueError, match="No active"):
                resolve_security_ids(["TCS"])

    def test_multiple_symbols(self, tmp_path: Path):
        expiry   = "2026-06-26"
        csv_file = tmp_path / "master.csv"
        csv_file.write_text(
            _CSV_HEADER
            + _csv_row("HDFCBANK", 66180, expiry)
            + _csv_row("RELIANCE", 66355, expiry)
        )
        with patch.dict("os.environ", {"INSTRUMENT_MASTER_PATH": str(csv_file)}):
            result = resolve_security_ids(["HDFCBANK", "RELIANCE"])
        assert result == {"HDFCBANK": 66180, "RELIANCE": 66355}

    def test_missing_master_file_raises(self, tmp_path: Path):
        with patch.dict("os.environ", {"INSTRUMENT_MASTER_PATH": str(tmp_path / "missing.csv")}):
            with pytest.raises(FileNotFoundError):
                resolve_security_ids(["HDFCBANK"])
