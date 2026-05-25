"""
PaperBroker — stateful per-instrument order simulator.

One PaperBroker instance per instrument. Fed depth packets and market
packets independently. Uses a 2-layer fill gate:

    Layer 1 (depth)  : bid_price_01 drops below posted BUY price
                       ask_price_01 rises above posted SELL price
    Layer 2 (market) : ltp confirms a trade printed at/beyond our level
                       AND market feed is not stale (< MARKET_FEED_STALE_SECS)

Queue position approximation (logged but not blocking):
    queue_ahead  = bid_qty_01 at time of post
    qty_consumed = cumulative drop in bid_qty_01 since post
    If fill confirmed but qty_consumed < queue_ahead → logged as 'queue_doubt'

No orders ever reach Dhan. All PnL is hypothetical.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from paper_trader.config import (
    LOT_SIZES, ENTRY_THRESHOLD, MAX_HOLD_PACKETS,
    ORDER_TIMEOUT_PKTS, N_LOTS, TICK_SIZE,
    MARKET_FEED_STALE_SECS,
)
from paper_trader.logger import log_trade, log_order_event, log_pnl_snapshot
from paper_trader.signal import compute_micro_deviation

IST = timezone(timedelta(hours=5, minutes=30))

# NSE futures fee schedule (Dhan)
_BROKERAGE_PER_ORDER  = 20.0
_STT_SELL             = 0.000125
_EXCHANGE_CHARGE      = 0.00002
_SEBI_FEE             = 0.000001
_STAMP_DUTY_BUY       = 0.00002
_GST_RATE             = 0.18


def _compute_fee(entry_price: float, exit_price: float,
                 lot_size: int, n_lots: int, direction: int) -> float:
    qty            = lot_size * n_lots
    entry_notional = entry_price * qty
    exit_notional  = exit_price  * qty
    buy_notional   = entry_notional if direction > 0 else exit_notional
    sell_notional  = exit_notional  if direction > 0 else entry_notional
    brokerage = _BROKERAGE_PER_ORDER * 2
    exchange  = (entry_notional + exit_notional) * _EXCHANGE_CHARGE
    sebi      = (entry_notional + exit_notional) * _SEBI_FEE
    stt       = sell_notional * _STT_SELL
    stamp     = buy_notional  * _STAMP_DUTY_BUY
    gst       = (brokerage + exchange + sebi) * _GST_RATE
    return round(brokerage + exchange + sebi + stt + stamp + gst, 2)


@dataclass
class _Order:
    side:          int      # +1 buy, -1 sell
    price:         float    # posted limit price
    post_packet:   int
    post_ts:       datetime
    queue_ahead:   float    # bid_qty_01 at time of post (proxy for queue depth)
    qty_consumed:  float = 0.0   # cumulative L1 qty drop since post
    role:          str = "entry"  # 'entry' or 'exit'


class PaperBroker:
    """
    Simulates one instrument's order flow in real time.
    Call on_depth_packet() on every depth websocket message.
    Call on_market_packet() on every market (LTP) websocket message.
    """

    def __init__(self, underlying: str) -> None:
        self.underlying  = underlying
        self.lot_size    = LOT_SIZES[underlying]

        # State
        self._packet_idx       = 0
        self._position_side    = 0       # 0 flat, +1 long, -1 short
        self._pending_entry: Optional[_Order] = None
        self._pending_exit:  Optional[_Order] = None
        self._entry_price      = 0.0
        self._entry_packet     = 0
        self._entry_ts: Optional[datetime] = None

        self._prev_abs_sig     = 0.0
        self._last_exit_packet = -(ORDER_TIMEOUT_PKTS + 1)

        # Market feed state
        self._ltp              = float("nan")
        self._ltt_utc: Optional[datetime] = None
        self._last_market_recv: Optional[datetime] = None

        # Accumulators
        self.n_posts  = 0
        self.n_fills  = 0
        self.n_cancels = 0
        self.cum_net_pnl = 0.0
        self.trades: list[dict] = []

        self._prev_bid_qty = float("nan")
        self._trading_date = ""
        self.last_mid      = 0.0   # most recent (bid+ask)/2; used by shutdown handler

    # ── Market feed ───────────────────────────────────────────────────────────

    def on_market_packet(self, ltp: float, ltt_utc: datetime,
                         recv_utc: datetime) -> None:
        self._ltp              = ltp
        self._ltt_utc          = ltt_utc
        self._last_market_recv = recv_utc

    # ── Depth feed (primary driver) ───────────────────────────────────────────

    def on_depth_packet(
        self,
        ts_utc:      datetime,
        bid_price:   float,
        bid_qty:     float,
        ask_price:   float,
        ask_qty:     float,
    ) -> None:
        i   = self._packet_idx
        self._packet_idx += 1
        mid = (bid_price + ask_price) / 2.0
        self.last_mid = mid
        sig = compute_micro_deviation(bid_price, bid_qty, ask_price, ask_qty)
        self._trading_date = ts_utc.astimezone(IST).strftime("%Y-%m-%d")

        # Track qty consumed at L1 for queue position estimate
        if not math.isnan(self._prev_bid_qty) and self._pending_entry is not None:
            drop = max(0.0, self._prev_bid_qty - bid_qty)
            self._pending_entry.qty_consumed += drop
        self._prev_bid_qty = bid_qty

        # ── STATE 1: idle ────────────────────────────────────────────────────
        if self._position_side == 0 and self._pending_entry is None:
            cross_ok    = self._prev_abs_sig < ENTRY_THRESHOLD
            cooldown_ok = (i - self._last_exit_packet) > ORDER_TIMEOUT_PKTS
            if cross_ok and cooldown_ok and abs(sig) >= ENTRY_THRESHOLD:
                side       = 1 if sig > 0 else -1
                post_price = bid_price if side > 0 else ask_price
                self._pending_entry = _Order(
                    side=side, price=post_price,
                    post_packet=i, post_ts=ts_utc,
                    queue_ahead=bid_qty if side > 0 else ask_qty,
                )
                self.n_posts += 1
                log_order_event(self.underlying, "post", side, post_price, self.lot_size * N_LOTS)

        # ── STATE 2: pending entry ────────────────────────────────────────────
        elif self._position_side == 0 and self._pending_entry is not None:
            o = self._pending_entry
            fill_candidate = (
                (o.side > 0 and bid_price < o.price) or
                (o.side < 0 and ask_price > o.price)
            )

            if fill_candidate and self._layer2_confirms(o.price, o.side):
                fill_layer = self._fill_layer(o)
                self._position_side   = o.side
                self._entry_price     = o.price
                self._entry_packet    = i
                self._entry_ts        = ts_utc
                self._pending_entry   = None
                self.n_fills         += 1
                log_order_event(self.underlying, "fill_confirmed", o.side,
                                o.price, self.lot_size * N_LOTS, fill_layer)

            elif (i - o.post_packet) >= ORDER_TIMEOUT_PKTS:
                self._pending_entry = None
                self.n_cancels     += 1
                log_order_event(self.underlying, "cancel", o.side, o.price,
                                self.lot_size * N_LOTS)

        # ── STATE 3 & 4: in position ─────────────────────────────────────────
        elif self._position_side != 0:
            packets_held = i - self._entry_packet

            # Post passive exit if not yet posted
            if self._pending_exit is None:
                side       = -self._position_side
                post_price = ask_price if self._position_side > 0 else bid_price
                self._pending_exit = _Order(
                    side=side, price=post_price,
                    post_packet=i, post_ts=ts_utc,
                    queue_ahead=ask_qty if side < 0 else bid_qty,
                    role="exit",
                )

            # Check exit fill
            ex = self._pending_exit
            exit_candidate = (
                (ex.side < 0 and ask_price > ex.price) or
                (ex.side > 0 and bid_price < ex.price)
            )
            if exit_candidate:
                self._close_position(ex.price, "maker_exit", i, ts_utc, mid)

            elif packets_held >= MAX_HOLD_PACKETS:
                taker_price = mid + (-self._position_side) * TICK_SIZE
                self._close_position(taker_price, "taker_max_hold", i, ts_utc, mid)

        self._prev_abs_sig = abs(sig)

    def eod_force_close(self, ts_utc: datetime, mid: float) -> None:
        if self._position_side != 0:
            taker_price = mid + (-self._position_side) * TICK_SIZE
            self._close_position(taker_price, "taker_eod",
                                 self._packet_idx, ts_utc, mid)

    def reset_session(self) -> None:
        self._packet_idx       = 0
        self._position_side    = 0
        self._pending_entry    = None
        self._pending_exit     = None
        self._prev_abs_sig     = 0.0
        self._last_exit_packet = -(ORDER_TIMEOUT_PKTS + 1)
        self._prev_bid_qty     = float("nan")

    # ── Internals ─────────────────────────────────────────────────────────────

    def _layer2_confirms(self, posted_price: float, side: int) -> bool:
        if self._last_market_recv is None or self._ltt_utc is None:
            return False
        age = (self._last_market_recv - self._ltt_utc).total_seconds()
        if age > MARKET_FEED_STALE_SECS:
            return False
        if side > 0:
            return self._ltp <= posted_price
        else:
            return self._ltp >= posted_price

    def _fill_layer(self, o: _Order) -> str:
        if not self._layer2_confirms(o.price, o.side):
            return "depth_only"
        if o.qty_consumed < o.queue_ahead:
            return "queue_doubt"
        return "depth+market"

    def _close_position(self, exit_price: float, method: str,
                        exit_packet: int, exit_ts: datetime, mid: float) -> None:
        qty       = self.lot_size * N_LOTS
        gross     = self._position_side * (exit_price - self._entry_price) * qty
        fee       = _compute_fee(self._entry_price, exit_price,
                                 self.lot_size, N_LOTS, self._position_side)
        net       = round(gross - fee, 2)
        hold_pkts = exit_packet - self._entry_packet

        trade = {
            "underlying":   self.underlying,
            "date":         self._trading_date,
            "direction":    self._position_side,
            "entry_ts":     self._entry_ts.isoformat() if self._entry_ts else "",
            "exit_ts":      exit_ts.isoformat(),
            "entry_price":  self._entry_price,
            "exit_price":   round(exit_price, 4),
            "entry_method": "maker",
            "exit_method":  method,
            "fill_layer":   "",   # set at fill time
            "lot_size":     self.lot_size,
            "n_lots":       N_LOTS,
            "notional":     round(self._entry_price * qty, 2),
            "hold_packets": hold_pkts,
            "hold_secs":    round(hold_pkts * 0.4, 1),
            "gross_pnl":    round(gross, 2),
            "fee":          fee,
            "net_pnl":      net,
            "queue_ahead":  "",
            "qty_consumed": "",
        }
        self.trades.append(trade)
        self.cum_net_pnl    += net
        self._position_side  = 0
        self._pending_exit   = None
        self._last_exit_packet = exit_packet

        log_trade(trade)
        log_pnl_snapshot(
            self.underlying, self._trading_date,
            self.cum_net_pnl, len(self.trades),
            self.n_posts, self.n_fills,
        )
