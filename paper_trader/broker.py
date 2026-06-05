"""
PaperBroker — stateful per-instrument order simulator.

One PaperBroker instance per instrument. Driven entirely by the depth feed.
Fill detection uses Layer 1 only (depth): BUY fills when bid_price_01 drops
below the posted price; SELL fills when ask_price_01 rises above it.
This matches the backtester's fill model exactly.

Economic edge gate (EDGE_MARGIN):
    Entry fires only when the half-spread the maker can capture covers its
    per-share round-trip fee with margin: spread/2 >= EDGE_MARGIN × fee/qty.
    This is per-instrument and price-aware; it replaces the old flat rupee
    threshold (which let TCS over-trade at a ~50% win rate while the cheap,
    high-win-rate banks barely traded).

Fill quality gate (QUEUE_FILL_MIN_FRAC):
    A fill is accepted only if qty_consumed >= QUEUE_FILL_MIN_FRAC × queue_ahead.
    queue_ahead  = L1 qty on the order's side at time of post
    qty_consumed = cumulative drop in that L1 qty since post
    Rejects noise bounces where the book moved back without consuming real depth.

Minimum hold (MIN_HOLD_PKTS):
    Passive exit is not posted until MIN_HOLD_PKTS packets after fill.
    Prevents immediate exit on the same-tick book bounce.

Robustness:
    Zero-price / crossed packets (Dhan emits these at session close) are dropped
    before any state update, so they cannot trigger spurious fills or corrupt the
    mid used by the end-of-day force-close. New entries also stop after
    NO_NEW_ENTRY_IST.

No orders ever reach Dhan. All PnL is hypothetical.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from paper_trader.config import (
    LOT_SIZES, ENTRY_THRESHOLD, MAX_HOLD_PACKETS,
    ORDER_TIMEOUT_PKTS, MIN_HOLD_PKTS, N_LOTS, TICK_SIZE,
    QUEUE_FILL_MIN_FRAC, EDGE_MARGIN, NO_NEW_ENTRY_IST,
    STOP_LOSS_TICKS,
)
from paper_trader.logger import default_logger
from paper_trader.signal import compute_micro_deviation

log = logging.getLogger("argus.broker")

IST = timezone(timedelta(hours=5, minutes=30))


class DayRisk:
    """
    Session-wide risk governor shared across all PaperBrokers in a process.

    Tracks aggregate day net PnL. Once it breaches loss_limit (a negative
    number), `halted` flips True and stays True — new entries are blocked for
    the rest of the session, but open positions still close normally. Because
    one process runs exactly one trading session (systemd start→stop), the
    object is naturally fresh each day; no explicit reset needed.
    """

    def __init__(self, loss_limit: float) -> None:
        self.loss_limit  = loss_limit
        self.day_net_pnl = 0.0
        self.halted      = False

    def record(self, net: float) -> None:
        self.day_net_pnl += net
        if not self.halted and self.day_net_pnl <= self.loss_limit:
            self.halted = True
            log.warning("DAILY LOSS LIMIT hit (%.0f ≤ %.0f) — halting new entries",
                        self.day_net_pnl, self.loss_limit)


@dataclass
class StrategyParams:
    """
    Per-arm strategy configuration. Defaults reproduce the live champion, so
    PaperBroker() with no params behaves exactly as before. Each parallel arm
    passes its own StrategyParams to vary stop / hold / margin / exit-mode etc.
    """
    entry_threshold:     float = ENTRY_THRESHOLD
    edge_margin:         float = EDGE_MARGIN
    max_hold_packets:    int   = MAX_HOLD_PACKETS
    order_timeout_pkts:  int   = ORDER_TIMEOUT_PKTS
    min_hold_pkts:       int   = MIN_HOLD_PKTS
    queue_fill_min_frac: float = QUEUE_FILL_MIN_FRAC
    stop_loss_ticks:     int   = STOP_LOSS_TICKS
    n_lots:              int   = N_LOTS
    tick_size:           float = TICK_SIZE
    no_new_entry_ist:    str   = NO_NEW_ENTRY_IST
    exit_mode:           str   = "maker"   # "maker" | "reversal"


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
    on_market_packet() is a no-op — fill detection uses depth only.
    """

    def __init__(self, underlying: str, risk: Optional[DayRisk] = None,
                 params: Optional[StrategyParams] = None,
                 lot_size: Optional[int] = None, logger=None) -> None:
        self.underlying  = underlying
        # lot_size is injected by the runner (resolved from the instrument
        # master); falls back to the static LOT_SIZES table for tests / the
        # original universe.
        self.lot_size    = lot_size if lot_size is not None else LOT_SIZES[underlying]
        self._risk       = risk                   # shared session risk governor
        self._logger     = logger or default_logger   # per-arm CSV logger
        self.p           = params or StrategyParams()
        h, m             = self.p.no_new_entry_ist.split(":")
        self._no_entry_after = (int(h), int(m))   # (hour, minute) IST cutoff

        # State
        self._packet_idx       = 0
        self._position_side    = 0       # 0 flat, +1 long, -1 short
        self._pending_entry: Optional[_Order] = None
        self._pending_exit:  Optional[_Order] = None
        self._entry_price      = 0.0
        self._entry_packet     = 0
        self._entry_ts: Optional[datetime] = None

        self._prev_abs_sig     = 0.0
        self._last_exit_packet = -(self.p.order_timeout_pkts + 1)

        # Accumulators
        self.n_posts   = 0
        self.n_fills   = 0
        self.n_cancels = 0
        self.cum_net_pnl = 0.0
        self.trades: list[dict] = []

        self._prev_bid_qty = float("nan")
        self._prev_ask_qty = float("nan")
        self._trading_date = ""
        self.last_mid      = 0.0   # most recent (bid+ask)/2; used by shutdown handler
        self._last_packet_ts: Optional[datetime] = None   # for monitor freshness

    # ── Live snapshot (for the monitor) ────────────────────────────────────────

    def snapshot(self, *, now: Optional[datetime] = None) -> dict:
        """Current live state for the monitoring dashboard. Read-only."""
        qty = self.lot_size * self.p.n_lots
        unrealized = 0.0
        if self._position_side != 0 and self.last_mid > 0.0:
            unrealized = self._position_side * (self.last_mid - self._entry_price) * qty
        age = None
        if self._last_packet_ts is not None:
            ref = now or datetime.now(timezone.utc)
            age = round((ref - self._last_packet_ts).total_seconds(), 2)
        return {
            "underlying":     self.underlying,
            "position_side":  self._position_side,
            "entry_price":    round(self._entry_price, 4) if self._position_side else None,
            "entry_ts":       self._entry_ts.isoformat() if (self._position_side and self._entry_ts) else None,
            "last_mid":       round(self.last_mid, 4),
            "qty":            qty,
            "unrealized_pnl": round(unrealized, 2),
            "pending_entry":  self._pending_entry.side if self._pending_entry else 0,
            "pending_exit":   self._pending_exit is not None,
            "n_posts":        self.n_posts,
            "n_fills":        self.n_fills,
            "n_cancels":      self.n_cancels,
            "n_trades":       len(self.trades),
            "realized_pnl":   round(self.cum_net_pnl, 2),
            "last_packet_age_sec": age,
        }

    # ── Market feed (no-op — Dhan only allows one connection per account) ──────

    def on_market_packet(self, ltp: float, ltt_utc: datetime,
                         recv_utc: datetime) -> None:
        pass

    # ── Depth feed (primary driver) ───────────────────────────────────────────

    def on_depth_packet(
        self,
        ts_utc:    datetime,
        bid_price: float,
        bid_qty:   float,
        ask_price: float,
        ask_qty:   float,
    ) -> None:
        # Drop garbage / session-end packets before touching any state. Dhan
        # emits zero-price packets around 15:30 IST; these would otherwise
        # trigger spurious fills (bid 0 < posted price) and zero out the mid
        # used by the EOD force-close, producing absurd PnL.
        if bid_price <= 0.0 or ask_price <= 0.0 or ask_price < bid_price:
            return

        self._last_packet_ts = ts_utc
        i   = self._packet_idx
        self._packet_idx += 1
        mid = (bid_price + ask_price) / 2.0
        self.last_mid = mid   # only ever a valid mid (garbage dropped above)
        sig = compute_micro_deviation(bid_price, bid_qty, ask_price, ask_qty)
        self._trading_date = ts_utc.astimezone(IST).strftime("%Y-%m-%d")

        # Track qty consumed on the order's side (bid for BUY, ask for SELL)
        if not math.isnan(self._prev_bid_qty) and self._pending_entry is not None:
            if self._pending_entry.side > 0:
                drop = max(0.0, self._prev_bid_qty - bid_qty)
            else:
                drop = max(0.0, self._prev_ask_qty - ask_qty)
            self._pending_entry.qty_consumed += drop
        self._prev_bid_qty = bid_qty
        self._prev_ask_qty = ask_qty

        # ── STATE 1: idle ────────────────────────────────────────────────────
        if self._position_side == 0 and self._pending_entry is None:
            # Economic edge gate: only quote when the half-spread we'd capture
            # covers our per-share round-trip fee with margin. Self-calibrating
            # per instrument from the live mid.
            qty          = self.lot_size * self.p.n_lots
            breakeven_sh = _compute_fee(mid, mid, self.lot_size, self.p.n_lots, 1) / qty
            half_spread  = (ask_price - bid_price) / 2.0

            ist          = ts_utc.astimezone(IST)
            cross_ok     = self._prev_abs_sig < self.p.entry_threshold
            cooldown_ok  = (i - self._last_exit_packet) > self.p.order_timeout_pkts
            signal_ok    = abs(sig) >= self.p.entry_threshold
            edge_ok      = half_spread >= self.p.edge_margin * breakeven_sh
            time_ok      = (ist.hour, ist.minute) < self._no_entry_after
            breaker_ok   = self._risk is None or not self._risk.halted

            if cross_ok and cooldown_ok and signal_ok and edge_ok and time_ok and breaker_ok:
                side       = 1 if sig > 0 else -1
                post_price = bid_price if side > 0 else ask_price
                self._pending_entry = _Order(
                    side=side, price=post_price,
                    post_packet=i, post_ts=ts_utc,
                    queue_ahead=bid_qty if side > 0 else ask_qty,
                )
                self.n_posts += 1
                self._logger.order_event(self.underlying, "post", side, post_price, self.lot_size * self.p.n_lots)

        # ── STATE 2: pending entry ────────────────────────────────────────────
        elif self._position_side == 0 and self._pending_entry is not None:
            o = self._pending_entry
            fill_candidate = (
                (o.side > 0 and bid_price < o.price) or
                (o.side < 0 and ask_price > o.price)
            )

            queue_ok = (
                o.queue_ahead <= 0 or
                o.qty_consumed >= self.p.queue_fill_min_frac * o.queue_ahead
            )
            if fill_candidate and queue_ok:
                self._position_side = o.side
                self._entry_price   = o.price
                self._entry_packet  = i
                self._entry_ts      = ts_utc
                self._pending_entry = None
                self.n_fills       += 1
                self._logger.order_event(self.underlying, "fill_confirmed", o.side,
                                         o.price, self.lot_size * self.p.n_lots, "depth_only")

            elif (i - o.post_packet) >= self.p.order_timeout_pkts:
                self._pending_entry = None
                self.n_cancels     += 1
                self._logger.order_event(self.underlying, "cancel", o.side, o.price,
                                         self.lot_size * self.p.n_lots)

        # ── STATE 3 & 4: in position ─────────────────────────────────────────
        elif self._position_side != 0:
            packets_held  = i - self._entry_packet
            adverse_ticks = self._position_side * (self._entry_price - mid) / self.p.tick_size

            # Hard price stop: bail at market if the position has run adverse
            # beyond stop_loss_ticks, rather than waiting for the time-based exit.
            if self.p.stop_loss_ticks > 0 and adverse_ticks >= self.p.stop_loss_ticks:
                taker_price = mid + (-self._position_side) * self.p.tick_size
                self._close_position(taker_price, "taker_stop", i, ts_utc, mid)

            # Signal-reversal exit (reversal mode only): the entry thesis is dead
            # — the microprice now points against the position. Bail at market.
            elif (self.p.exit_mode == "reversal"
                  and self._position_side * sig < 0
                  and abs(sig) >= self.p.entry_threshold):
                taker_price = mid + (-self._position_side) * self.p.tick_size
                self._close_position(taker_price, "taker_reversal", i, ts_utc, mid)

            else:
                # Post passive exit only after minimum hold period
                if self._pending_exit is None and packets_held >= self.p.min_hold_pkts:
                    side       = -self._position_side
                    post_price = ask_price if self._position_side > 0 else bid_price
                    self._pending_exit = _Order(
                        side=side, price=post_price,
                        post_packet=i, post_ts=ts_utc,
                        queue_ahead=ask_qty if side < 0 else bid_qty,
                        role="exit",
                    )

                # Check exit fill or taker fallback
                if self._pending_exit is not None:
                    ex = self._pending_exit
                    exit_candidate = (
                        (ex.side < 0 and ask_price > ex.price) or
                        (ex.side > 0 and bid_price < ex.price)
                    )
                    if exit_candidate:
                        self._close_position(ex.price, "maker_exit", i, ts_utc, mid)
                    elif packets_held >= self.p.max_hold_packets:
                        taker_price = mid + (-self._position_side) * self.p.tick_size
                        self._close_position(taker_price, "taker_max_hold", i, ts_utc, mid)
                elif packets_held >= self.p.max_hold_packets:
                    taker_price = mid + (-self._position_side) * self.p.tick_size
                    self._close_position(taker_price, "taker_max_hold", i, ts_utc, mid)

        self._prev_abs_sig = abs(sig)

    def eod_force_close(self, ts_utc: datetime, mid: float) -> None:
        if self._position_side != 0:
            # Defend against a zero/garbage mid: fall back to the last valid mid,
            # then to the entry price (≈ flat PnL) rather than emitting nonsense.
            close_mid = mid if (mid and mid > 0.0) else self.last_mid
            if not close_mid or close_mid <= 0.0:
                close_mid = self._entry_price
            taker_price = close_mid + (-self._position_side) * self.p.tick_size
            self._close_position(taker_price, "taker_eod",
                                 self._packet_idx, ts_utc, close_mid)

    def reset_session(self) -> None:
        self._packet_idx       = 0
        self._position_side    = 0
        self._pending_entry    = None
        self._pending_exit     = None
        self._prev_abs_sig     = 0.0
        self._last_exit_packet = -(self.p.order_timeout_pkts + 1)
        self._prev_bid_qty     = float("nan")
        self._prev_ask_qty     = float("nan")

    # ── Internals ─────────────────────────────────────────────────────────────

    def _close_position(self, exit_price: float, method: str,
                        exit_packet: int, exit_ts: datetime, mid: float) -> None:
        qty       = self.lot_size * self.p.n_lots
        gross     = self._position_side * (exit_price - self._entry_price) * qty
        fee       = _compute_fee(self._entry_price, exit_price,
                                 self.lot_size, self.p.n_lots, self._position_side)
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
            "fill_layer":   "depth_only",
            "lot_size":     self.lot_size,
            "n_lots":       self.p.n_lots,
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
        if self._risk is not None:
            self._risk.record(net)
        self._position_side  = 0
        self._pending_exit   = None
        self._last_exit_packet = exit_packet

        self._logger.trade(trade)
        self._logger.pnl_snapshot(
            self.underlying, self._trading_date,
            self.cum_net_pnl, len(self.trades),
            self.n_posts, self.n_fills,
        )
