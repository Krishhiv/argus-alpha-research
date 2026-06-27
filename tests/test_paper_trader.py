"""
Unit tests for paper_trader modules.

Covers: signal math, PaperBroker state machine, binary packet parser,
contract resolution. No network, no filesystem side effects (tmp_path for CSV).
"""

from __future__ import annotations

import struct
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from paper_trader.signal import compute_micro_deviation
from paper_trader.broker import PaperBroker, DayRisk, StrategyParams, _compute_fee
from paper_trader.dhan_parser import (
    DepthSidePacket,
    TickerPacket,
    PacketParseError,
    parse_depth_feed_message,
    parse_market_feed_message,
)
from paper_trader.contracts import resolve_security_ids
from paper_trader.config import (
    ENTRY_THRESHOLD, ORDER_TIMEOUT_PKTS, MAX_HOLD_PACKETS, MIN_HOLD_PKTS, STOP_LOSS_TICKS,
)


# ── Shared helpers ────────────────────────────────────────────────────────────

UTC = timezone.utc

# Fixed in-session timestamp (09:30 IST) so the NO_NEW_ENTRY_IST cutoff does not
# interfere with the bulk of the tests regardless of wall-clock time at run.
SESSION_TS = datetime(2026, 6, 1, 4, 0, 0, tzinfo=UTC)


def _depth(broker: PaperBroker, bid_p: float, bid_q: int, ask_p: float, ask_q: int,
           ts: datetime = SESSION_TS) -> None:
    broker.on_depth_packet(
        ts_utc=ts, bid_price=bid_p, bid_qty=bid_q,
        ask_price=ask_p, ask_qty=ask_q,
    )


# Signal helpers ---------------------------------------------------------------
# Books use a 1.0 spread (half-spread 0.5) so the economic edge gate passes for
# the cheap test instrument (HDFCBANK break-even ≈ 0.11/share at price 100).
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


# ── Broker — fill detection (depth-only) ─────────────────────────────────────

class TestBrokerFill:
    def _broker_pending_buy(self) -> PaperBroker:
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY)           # posts BUY at 100.0
        return br

    def test_buy_fill_depth_only(self):
        br = self._broker_pending_buy()
        _depth(br, 99.5, 500, 101.0, 10)        # bid < 100.0 → fill
        assert br.n_fills == 1
        assert br._position_side == 1

    def test_sell_fill(self):
        br = PaperBroker("HDFCBANK")
        _depth(br, *_SELL)                       # posts SELL at 101.0
        _depth(br, 100.0, 10, 101.5, 500)        # ask > 101.0 → fill
        assert br.n_fills == 1
        assert br._position_side == -1


# ── Broker — exit logic ───────────────────────────────────────────────────────

class TestBrokerExit:
    def _filled_long(self) -> PaperBroker:
        """Returns a broker that has just filled a long at 100.0."""
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY)                        # post BUY at 100.0
        _depth(br, 99.5, 500, 101.0, 10)         # bid < 100.0 → fill
        assert br.n_fills == 1
        return br

    def test_maker_exit_long(self):
        br = self._filled_long()
        # Exit is posted only after MIN_HOLD_PKTS packets in position
        for _ in range(MIN_HOLD_PKTS):
            _depth(br, 100.0, 500, 102.0, 500)  # hold; exit posts at ask=102.0 on packet 10
        # Trigger: ask=102.1 > exit_price=102.0 → maker_exit
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
        for _ in range(MIN_HOLD_PKTS):
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

class TestBrokerQueueFill:
    def test_queue_filter_blocks_insufficient_consumption(self):
        # qty_consumed=100 < 10% of queue_ahead=10000 → fill rejected
        br = PaperBroker("HDFCBANK")
        _depth(br, 100.0, 10_000, 101.0, 10)    # post BUY, queue_ahead=10_000
        _depth(br, 99.5, 9_900, 101.0, 10)      # qty_consumed=100, need ≥1000 → blocked
        assert br.n_fills == 0
        assert br._pending_entry is not None     # order still live, not cancelled

    def test_queue_filter_allows_sufficient_consumption(self):
        # qty_consumed=490 >= 10% of queue_ahead=990 → fill accepted
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY)                        # post BUY at 100.0, queue_ahead=990
        _depth(br, 99.5, 500, 101.0, 10)         # qty_consumed=490 ≥ 99 → fill
        assert br.n_fills == 1

    def test_queue_filter_sell_side(self):
        # For SELL orders, ask_qty drop (not bid_qty) is checked
        br = PaperBroker("HDFCBANK")
        _depth(br, *_SELL)                       # post SELL at 101.0, queue_ahead=990 (ask_qty)
        _depth(br, 100.0, 10, 101.5, 500)        # ask_qty drop: 990→500=490 ≥ 99 → fill
        assert br.n_fills == 1


# ── Broker — economic edge gate ───────────────────────────────────────────────

class TestBrokerEconomicGate:
    def test_blocks_when_half_spread_below_breakeven(self):
        # TCS break-even ≈ ₹0.72/share. A 1.0 spread (half 0.5) does not cover it,
        # even though the microprice signal itself is strong.
        br = PaperBroker("TCS")
        assert abs(compute_micro_deviation(2460.0, 990, 2461.0, 10)) >= ENTRY_THRESHOLD
        _depth(br, 2460.0, 990, 2461.0, 10)      # half_spread 0.5 < 0.72 → blocked
        assert br.n_posts == 0

    def test_allows_when_half_spread_covers_fees(self):
        # A 2.0 spread (half 1.0) on TCS clears the ~0.72 break-even.
        br = PaperBroker("TCS")
        _depth(br, 2460.0, 990, 2462.0, 10)      # half_spread 1.0 ≥ 0.72 → post
        assert br.n_posts == 1
        assert br._pending_entry.side == 1

    def test_cheap_instrument_trades_at_tight_spread(self):
        # HDFCBANK break-even ≈ ₹0.23/share, so the same 1.0 spread that blocks
        # TCS lets HDFCBANK quote. This is the cross-instrument self-calibration.
        br = PaperBroker("HDFCBANK")
        _depth(br, 743.0, 990, 744.0, 10)        # half_spread 0.5 ≥ 0.23 → post
        assert br.n_posts == 1


# ── Broker — garbage / session-end packet guard ──────────────────────────────

class TestBrokerGarbagePackets:
    def test_zero_price_packet_no_post(self):
        br = PaperBroker("HDFCBANK")
        _depth(br, 0.0, 990, 0.0, 10)            # Dhan session-end zero packet
        assert br.n_posts == 0
        assert br.last_mid == 0.0                # mid never corrupted

    def test_zero_price_packet_does_not_fill_or_corrupt_mid(self):
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY)                        # post BUY at 100.0; last_mid = 100.5
        _depth(br, 0.0, 0, 0.0, 0)               # garbage — must be dropped
        assert br.n_fills == 0                   # no spurious fill (bid 0 < 100)
        assert br._pending_entry is not None
        assert br.last_mid == pytest.approx(100.5)   # mid unchanged by garbage

    def test_crossed_book_dropped(self):
        br = PaperBroker("HDFCBANK")
        _depth(br, 101.0, 990, 100.0, 10)        # ask < bid → dropped
        assert br.n_posts == 0
        assert br.last_mid == 0.0


# ── Broker — session entry cutoff ─────────────────────────────────────────────

class TestBrokerSessionCutoff:
    _LATE  = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)   # 15:30 IST (past cutoff)
    _EARLY = datetime(2026, 6, 1,  9, 50, 0, tzinfo=UTC)  # 15:20 IST (before cutoff)

    def test_no_entry_after_cutoff(self):
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY, ts=self._LATE)
        assert br.n_posts == 0

    def test_entry_allowed_before_cutoff(self):
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY, ts=self._EARLY)
        assert br.n_posts == 1


# ── Broker — robust end-of-day force close ────────────────────────────────────

class TestBrokerRobustEOD:
    def _filled_long(self) -> PaperBroker:
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY)                        # post BUY at 100.0
        _depth(br, 99.5, 500, 101.0, 10)         # fill; last_mid = 100.25
        assert br.n_fills == 1
        return br

    def test_zero_mid_falls_back_to_last_valid_mid(self):
        br = self._filled_long()
        br.eod_force_close(ts_utc=datetime.now(UTC), mid=0.0)
        assert br._position_side == 0
        t = br.trades[0]
        assert t["exit_method"] == "taker_eod"
        # Falls back to last valid mid 100.25, NOT a garbage 0 ± tick
        assert t["exit_price"] == pytest.approx(100.20)
        assert abs(t["net_pnl"]) < 1_000          # sane, not a ₹400k artifact

    def test_falls_back_to_entry_price_when_no_valid_mid(self):
        br = self._filled_long()
        br.last_mid = 0.0                         # simulate never having a valid mid
        br.eod_force_close(ts_utc=datetime.now(UTC), mid=0.0)
        t = br.trades[0]
        assert t["exit_price"] == pytest.approx(99.95)   # entry 100.0 − tick
        assert abs(t["net_pnl"]) < 1_000


# ── Broker — hard price stop ──────────────────────────────────────────────────

class TestBrokerStopLoss:
    def _filled_long(self) -> PaperBroker:
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY)                         # post BUY at 100.0
        _depth(br, 99.5, 500, 101.0, 10)          # fill long @ 100.0
        assert br.n_fills == 1
        return br

    def test_stop_fires_on_large_adverse_move(self):
        # STOP_LOSS_TICKS=12 → 0.60 below entry. mid=99.1 is 18 ticks adverse.
        br = self._filled_long()
        _depth(br, 99.0, 500, 99.2, 500)          # mid 99.1 → adverse 18 ticks ≥ 12
        assert br._position_side == 0
        assert br.trades[0]["exit_method"] == "taker_stop"
        assert br.trades[0]["net_pnl"] < 0

    def test_stop_does_not_fire_on_small_wobble(self):
        # mid 99.85 is only 3 ticks adverse — below the 12-tick stop, must hold.
        br = self._filled_long()
        _depth(br, 99.8, 500, 99.9, 500)          # mid 99.85 → 3 ticks adverse
        assert br._position_side == 1             # still in position
        assert all(t["exit_method"] != "taker_stop" for t in br.trades)

    def test_stop_threshold_is_active(self):
        assert STOP_LOSS_TICKS > 0                # config sanity — stop is enabled


# ── Broker — per-arm StrategyParams + reversal exit ───────────────────────────

class TestStrategyParams:
    def _open_long(self, params: StrategyParams) -> PaperBroker:
        br = PaperBroker("HDFCBANK", params=params)
        _depth(br, *_BUY)                          # post long @ 100.0
        _depth(br, 99.5, 500, 101.0, 10)           # fill long @ 100.0
        assert br.n_fills == 1
        return br

    def test_custom_stop_disabled(self):
        # stop_loss_ticks=0 → a large adverse move does NOT stop out
        br = self._open_long(StrategyParams(stop_loss_ticks=0))
        _depth(br, 99.0, 500, 99.2, 500)           # mid 99.1 → 18 ticks adverse
        assert br._position_side == 1              # still in position, no stop
        assert all(t["exit_method"] != "taker_stop" for t in br.trades)

    def test_reversal_exit_fires_on_signal_flip(self):
        br = self._open_long(StrategyParams(exit_mode="reversal"))
        # ask-heavy book → micro_deviation strongly negative, opposes the long
        _depth(br, 100.0, 10, 101.0, 990)          # sig ≈ −0.49
        assert br._position_side == 0
        assert br.trades[0]["exit_method"] == "taker_reversal"

    def test_maker_mode_holds_through_signal_flip(self):
        # Same flip, but default maker mode does NOT reversal-exit
        br = self._open_long(StrategyParams())     # exit_mode="maker"
        _depth(br, 100.0, 10, 101.0, 990)
        assert br._position_side == 1              # still long
        assert all(t["exit_method"] != "taker_reversal" for t in br.trades)

    def test_params_default_matches_config(self):
        p = StrategyParams()
        assert p.stop_loss_ticks == STOP_LOSS_TICKS and p.max_hold_packets == MAX_HOLD_PACKETS


# ── Broker — queue-aware EXIT fill (Expenture I realistic fills) ───────────────

class TestBrokerQueueExitFill:
    def _open_long(self, params: StrategyParams) -> PaperBroker:
        br = PaperBroker("HDFCBANK", params=params)
        _depth(br, *_BUY)                          # post long @ 100.0
        _depth(br, 99.5, 500, 101.0, 10)           # fill long @ 100.0
        assert br.n_fills == 1
        return br

    def _post_exit_at_102(self, br: PaperBroker) -> None:
        # hold past MIN_HOLD_PKTS so the passive exit posts at ask=102.0 with
        # queue_ahead=1000; price favourable so the stop never fires.
        for _ in range(MIN_HOLD_PKTS):
            _depth(br, 100.0, 500, 102.0, 1000)
        assert br._pending_exit is not None and br._pending_exit.queue_ahead == 1000

    def test_default_off_fills_on_touch_without_queue(self):
        # Basecamp model: queue_exit_fill=False → touch fills even with no volume
        br = self._open_long(StrategyParams())     # default: queue_exit_fill False
        self._post_exit_at_102(br)
        _depth(br, 100.0, 500, 102.1, 1000)        # touch, zero qty consumed
        assert br.trades[0]["exit_method"] == "maker_exit"

    def test_queue_exit_blocks_touch_until_cleared(self):
        # Realistic model: touch alone does NOT fill — queue must clear first
        br = self._open_long(StrategyParams(queue_exit_fill=True, queue_exit_min_frac=1.0))
        self._post_exit_at_102(br)
        for _ in range(MAX_HOLD_PACKETS + 1):
            _depth(br, 100.0, 500, 102.1, 1000)    # touch, but ask_qty never drops
        assert br._position_side == 0
        assert br.trades[0]["exit_method"] == "taker_max_hold"   # fell to taker

    def test_queue_exit_fills_when_full_queue_clears(self):
        br = self._open_long(StrategyParams(queue_exit_fill=True, queue_exit_min_frac=1.0))
        self._post_exit_at_102(br)
        _depth(br, 100.0, 500, 102.1, 0)           # ask_qty 1000→0: consumed 1000 ≥ 1000
        assert br._position_side == 0
        assert br.trades[0]["exit_method"] == "maker_exit"

    def test_queue_exit_half_frac_fills_on_half_clear(self):
        br = self._open_long(StrategyParams(queue_exit_fill=True, queue_exit_min_frac=0.5))
        self._post_exit_at_102(br)
        _depth(br, 100.0, 500, 102.1, 500)         # consumed 500 ≥ 0.5×1000
        assert br.trades[0]["exit_method"] == "maker_exit"

    def test_queue_ahead_recorded_in_trade(self):
        br = self._open_long(StrategyParams())
        self._post_exit_at_102(br)
        _depth(br, 100.0, 500, 102.1, 800)         # ask 1000→800, consumed 200
        t = br.trades[0]
        assert t["queue_ahead"] == 1000
        assert t["qty_consumed"] == 200


# ── Broker — entry signal-context logging (Expenture I observability) ──────────

class TestBrokerEntryContext:
    def test_entry_context_captured_in_trade(self):
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY)                          # post: dev>0, spread 1.0 (101−100)
        _depth(br, 99.5, 500, 101.0, 10)           # fill long @ 100.0
        for _ in range(MIN_HOLD_PKTS):
            _depth(br, 100.0, 500, 102.0, 500)
        _depth(br, 100.0, 500, 102.1, 500)         # maker exit
        t = br.trades[0]
        assert t["entry_sig"] > 0                  # bid-heavy book → +dev captured
        assert t["entry_spread"] == pytest.approx(1.0)   # ask−bid at entry post
        assert t["edge_ratio"] > 0                 # half-spread cleared fee with margin

    def test_taker_exit_has_blank_queue_but_keeps_context(self):
        br = PaperBroker("HDFCBANK")
        _depth(br, *_BUY)
        _depth(br, 99.5, 500, 101.0, 10)           # fill long
        _depth(br, 99.0, 500, 99.2, 500)           # 18-tick adverse → taker_stop
        t = br.trades[0]
        assert t["exit_method"] == "taker_stop"
        assert t["queue_ahead"] == ""              # no maker exit order existed
        assert t["entry_sig"] > 0                  # entry context still present


# ── Risk — daily loss circuit breaker ─────────────────────────────────────────

class TestDayRiskCircuitBreaker:
    def test_halts_when_limit_breached(self):
        risk = DayRisk(loss_limit=-7500.0)
        risk.record(-5000.0)
        assert not risk.halted
        risk.record(-3000.0)                      # cumulative −8000 ≤ −7500
        assert risk.halted

    def test_stays_halted(self):
        risk = DayRisk(loss_limit=-7500.0)
        risk.record(-8000.0)
        assert risk.halted
        risk.record(+10000.0)                     # recovery does NOT un-halt
        assert risk.halted

    def test_halted_risk_blocks_new_entries(self):
        risk = DayRisk(loss_limit=-7500.0)
        risk.record(-8000.0)
        br = PaperBroker("ICICIBANK", risk=risk)
        _depth(br, *_BUY)                          # strong signal, but halted
        assert br.n_posts == 0

    def test_active_risk_allows_entries(self):
        risk = DayRisk(loss_limit=-7500.0)
        br = PaperBroker("HDFCBANK", risk=risk)
        _depth(br, *_BUY)
        assert br.n_posts == 1

    def test_broker_records_pnl_to_risk(self):
        risk = DayRisk(loss_limit=-7500.0)
        br = PaperBroker("HDFCBANK", risk=risk)
        _depth(br, *_BUY)                          # post
        _depth(br, 99.5, 500, 101.0, 10)           # fill long @ 100.0
        br.eod_force_close(ts_utc=datetime.now(UTC), mid=100.25)
        assert risk.day_net_pnl == pytest.approx(br.cum_net_pnl)
        assert len(br.trades) == 1


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
        near   = (datetime.now(UTC).date() + timedelta(days=7)).isoformat()
        far    = (datetime.now(UTC).date() + timedelta(days=35)).isoformat()
        csv_file = tmp_path / "master.csv"
        csv_file.write_text(
            _CSV_HEADER
            + _csv_row("HDFCBANK", 66180, near)
            + _csv_row("HDFCBANK", 67000, far)   # next month
        )
        with patch.dict("os.environ", {"INSTRUMENT_MASTER_PATH": str(csv_file)}):
            result = resolve_security_ids(["HDFCBANK"])
        assert result["HDFCBANK"] == 66180   # picks nearest non-expired, not later

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
        expiry   = (datetime.now(UTC).date() + timedelta(days=30)).isoformat()
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
