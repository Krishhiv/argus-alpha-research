"""
Maker (passive limit order) backtester for NSE depth-feed signals.

Posts passive limit orders at L1 (best bid/ask) based on a directional signal,
then waits for an aggressor to hit the order. Filling as a maker means
*receiving* the bid (for a buy) or *paying* the ask (for a sell) — i.e., earning
the spread rather than paying it.

State machine per packet
------------------------
    idle              → if signal exceeds threshold, post entry limit at L1
    pending_entry     → check fill (level disappeared) or cancel (timeout)
    in_position       → if exit_mode == 'maker', post passive exit on opposite side
    pending_exit      → check fill, or taker exit at max_hold

Fill rule
---------
A BUY posted at price B is considered filled when at any later packet the best
bid drops BELOW B — meaning aggressive sells consumed the level we were resting
on. Symmetric for SELL at ask. This is conservative: it assumes we were at the
back of the queue and only fill when the level is fully exhausted.

Exit modes
----------
- 'taker': always cross the spread at max_hold (simple, costs ~1 tick of slippage).
- 'maker': try to fill passively on the opposite side; fall back to taker after
           max_hold packets. Captures the full spread when both legs fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Literal

import numpy as np
import pandas as pd

TICK_SIZE: float = 0.05


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------

@dataclass
class MakerCostModel:
    """
    Same NSE futures explicit fee schedule as the taker model. The difference
    is that slippage is only paid on the taker leg of a round trip; maker fills
    receive the L1 price exactly (no slippage).
    """
    brokerage_per_order:  float = 20.0       # ₹ flat per order
    stt_sell:             float = 0.000125   # 0.0125% sell side
    exchange_charge:      float = 0.00002    # 0.002%  both sides
    sebi_fee:             float = 0.000001   # 0.0001% both sides
    stamp_duty_buy:       float = 0.00002    # 0.002%  buy side
    gst_rate:             float = 0.18       # 18% on brokerage + exchange + SEBI
    taker_slippage_ticks: int   = 1          # ticks paid on the taker leg only

    def effective_taker_exit_price(self, midprice: float, direction: int) -> float:
        """
        Cross-the-spread exit price. A long exit sells at mid − slippage; a
        short exit buys at mid + slippage. `direction` is the position side.
        """
        sign = -direction
        return midprice + sign * self.taker_slippage_ticks * TICK_SIZE

    def fee_cost(
        self,
        entry_price: float,
        exit_price:  float,
        lot_size:    float,
        n_lots:      int,
        direction:   int,
    ) -> float:
        """Explicit regulatory + brokerage charges for one round trip."""
        qty            = lot_size * n_lots
        entry_notional = entry_price * qty
        exit_notional  = exit_price  * qty

        buy_notional  = entry_notional if direction > 0 else exit_notional
        sell_notional = exit_notional  if direction > 0 else entry_notional

        brokerage = self.brokerage_per_order * 2
        exchange  = (entry_notional + exit_notional) * self.exchange_charge
        sebi      = (entry_notional + exit_notional) * self.sebi_fee
        stt       = sell_notional * self.stt_sell
        stamp     = buy_notional  * self.stamp_duty_buy
        gst       = (brokerage + exchange + sebi) * self.gst_rate

        return brokerage + exchange + sebi + stt + stamp + gst


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MakerOrder:
    """A passive limit order resting on the book."""
    side:        int      # +1 buy, -1 sell
    post_packet: int      # packet at which posted
    post_price:  float    # posted limit price (L1 bid for buy, L1 ask for sell)
    post_mid:    float    # midprice at post time
    role:        str      # 'entry' or 'exit'


@dataclass
class MakerTrade:
    """A completed round trip — passive entry + (maker or taker) exit."""
    direction:    int             # +1 long, -1 short
    entry_packet: int
    exit_packet:  int
    entry_ts:     pd.Timestamp
    exit_ts:      pd.Timestamp
    entry_price:  float           # passive fill price (no slippage)
    exit_price:   float           # passive fill or mid ± taker slippage
    entry_method: str             # always 'maker'
    exit_method:  str             # 'maker_exit' | 'taker_max_hold' | 'taker_eod'
    lot_size:     float
    n_lots:       int
    gross_pnl:    float           # direction × (exit_price − entry_price) × qty
    fee:          float           # explicit charges
    net_pnl:      float           # gross_pnl − fee
    exit_reason:  str             # same as exit_method (kept for parity with taker engine)


@dataclass
class MakerBacktestResult:
    trades:         list[MakerTrade]
    cumulative_pnl: pd.Series   # net PnL cumulated per packet, indexed by ts_ist
    n_posts:        int          # entry orders posted (filled + cancelled)
    n_fills:        int          # entry orders that filled
    n_cancels:      int          # entry orders cancelled (timed out)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def fill_rate(self) -> float:
        return self.n_fills / self.n_posts if self.n_posts > 0 else float("nan")

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades)

    @property
    def total_fees(self) -> float:
        return sum(t.fee for t in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return float("nan")
        return sum(1 for t in self.trades if t.net_pnl > 0) / len(self.trades)

    @property
    def profit_factor(self) -> float:
        wins   = sum(t.net_pnl for t in self.trades if t.net_pnl > 0)
        losses = abs(sum(t.net_pnl for t in self.trades if t.net_pnl <= 0))
        return wins / losses if losses > 0 else float("inf")

    @property
    def maker_exit_rate(self) -> float:
        if not self.trades:
            return float("nan")
        return sum(1 for t in self.trades if t.exit_method == "maker_exit") / len(self.trades)

    @property
    def max_drawdown(self) -> float:
        peak = self.cumulative_pnl.cummax()
        return float((self.cumulative_pnl - peak).min())

    def exit_method_counts(self) -> dict:
        counts: dict = {}
        for t in self.trades:
            counts[t.exit_method] = counts.get(t.exit_method, 0) + 1
        return counts

    def summary(self) -> dict:
        return {
            "n_posts":         self.n_posts,
            "n_fills":         self.n_fills,
            "fill_rate":       round(self.fill_rate, 4),
            "n_trades":        self.n_trades,
            "maker_exit_rate": round(self.maker_exit_rate, 4) if self.trades else float("nan"),
            "net_pnl":         round(self.net_pnl, 2),
            "gross_pnl":       round(self.gross_pnl, 2),
            "total_fees":      round(self.total_fees, 2),
            "win_rate":        round(self.win_rate, 4) if self.trades else float("nan"),
            "profit_factor":   round(self.profit_factor, 4),
            "max_drawdown":    round(self.max_drawdown, 2),
        }


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

class MakerBacktester:
    """
    Packet-by-packet event-driven maker backtester.

    Parameters
    ----------
    signal_col       : Column used for entry decisions. Positive → buy, negative → sell.
    entry_threshold  : Minimum |signal| to post an entry order.
    max_hold         : After entry fill, packets to wait before forcing a taker exit.
    order_timeout    : Packets to wait for an entry fill before cancelling the order.
    exit_mode        : 'maker' attempts passive exit then falls back to taker; 'taker'
                       always crosses the spread at max_hold.
    cooldown         : Packets to wait after a trade closes before posting again.
    fresh_cross      : If True, require |signal| to have crossed the threshold from
                       below (prev_abs_sig < threshold ≤ abs_sig). Prevents re-posting
                       while the signal is sustained.
    lot_size         : Shares per lot (instrument-specific; HDFCBANK = 550).
    n_lots           : Number of lots per trade.
    cost_model       : MakerCostModel instance. Defaults to standard NSE rates.
    """

    def __init__(
        self,
        signal_col:      str,
        entry_threshold: float,
        max_hold:        int,
        order_timeout:   int                       = 20,
        exit_mode:       Literal["taker", "maker"] = "maker",
        cooldown:        int                       = 0,
        fresh_cross:     bool                      = False,
        lot_size:        float                     = 550.0,
        n_lots:          int                       = 1,
        cost_model:      Optional[MakerCostModel]  = None,
    ):
        if exit_mode not in ("taker", "maker"):
            raise ValueError(f"exit_mode must be 'taker' or 'maker', got {exit_mode!r}")

        self.signal_col      = signal_col
        self.entry_threshold = entry_threshold
        self.max_hold        = max_hold
        self.order_timeout   = order_timeout
        self.exit_mode       = exit_mode
        self.cooldown        = cooldown
        self.fresh_cross     = fresh_cross
        self.lot_size        = lot_size
        self.n_lots          = n_lots
        self.costs           = cost_model or MakerCostModel()

    def run(self, df: pd.DataFrame) -> MakerBacktestResult:
        """
        Required columns: ts_ist, midprice, bid_price_01, ask_price_01, self.signal_col.
        """
        required = ["ts_ist", "midprice", "bid_price_01", "ask_price_01", self.signal_col]
        missing  = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"MakerBacktester requires columns: {missing}")

        signal = df[self.signal_col].to_numpy(float)
        bid_p  = df["bid_price_01"].to_numpy(float)
        ask_p  = df["ask_price_01"].to_numpy(float)
        mid    = df["midprice"].to_numpy(float)
        ts     = df["ts_ist"].to_numpy()
        n      = len(df)

        trades:        list[MakerTrade] = []
        pnl_by_packet: np.ndarray = np.zeros(n)

        # State
        pending_entry: Optional[MakerOrder] = None
        pending_exit:  Optional[MakerOrder] = None
        position_side:         int    = 0            # 0 = flat, +1 = long, -1 = short
        position_entry_packet: int    = 0
        position_entry_price:  float  = 0.0
        position_entry_ts:     object = None

        last_exit_packet = -(self.cooldown + 1)
        prev_abs_sig     = 0.0

        n_posts   = 0
        n_fills   = 0
        n_cancels = 0

        for i in range(n):
            sig = signal[i]

            if (np.isnan(sig) or np.isnan(mid[i])
                    or np.isnan(bid_p[i]) or np.isnan(ask_p[i])):
                prev_abs_sig = 0.0
                continue

            abs_sig = abs(sig)

            # =========================================================
            # STATE 1: idle — consider posting an entry order
            # =========================================================
            if position_side == 0 and pending_entry is None:
                cooldown_ok = (i - last_exit_packet) > self.cooldown
                cross_ok    = (not self.fresh_cross) or (prev_abs_sig < self.entry_threshold)

                if cooldown_ok and cross_ok and abs_sig >= self.entry_threshold:
                    side       = 1 if sig > 0 else -1
                    post_price = bid_p[i] if side > 0 else ask_p[i]
                    pending_entry = MakerOrder(
                        side        = side,
                        post_packet = i,
                        post_price  = post_price,
                        post_mid    = mid[i],
                        role        = "entry",
                    )
                    n_posts += 1

            # =========================================================
            # STATE 2: pending entry — check fill or cancel
            # =========================================================
            elif position_side == 0 and pending_entry is not None:
                packets_since_post = i - pending_entry.post_packet
                filled = False

                if pending_entry.side > 0:
                    # BUY at bid: filled when best bid drops below our post price
                    if bid_p[i] < pending_entry.post_price:
                        filled = True
                else:
                    # SELL at ask: filled when best ask rises above our post price
                    if ask_p[i] > pending_entry.post_price:
                        filled = True

                if filled:
                    position_side         = pending_entry.side
                    position_entry_packet = i
                    position_entry_price  = pending_entry.post_price
                    position_entry_ts     = ts[i]
                    pending_entry         = None
                    n_fills              += 1

                elif packets_since_post >= self.order_timeout:
                    pending_entry = None
                    n_cancels    += 1

            # =========================================================
            # STATE 3 & 4: in position — manage exit
            # =========================================================
            elif position_side != 0:
                packets_held = i - position_entry_packet

                # Post passive exit if maker mode and not yet posted
                if self.exit_mode == "maker" and pending_exit is None:
                    if position_side > 0:
                        # Long: post SELL at ask
                        pending_exit = MakerOrder(
                            side=-1, post_packet=i, post_price=ask_p[i],
                            post_mid=mid[i], role="exit",
                        )
                    else:
                        # Short: post BUY at bid
                        pending_exit = MakerOrder(
                            side=+1, post_packet=i, post_price=bid_p[i],
                            post_mid=mid[i], role="exit",
                        )

                # Check maker exit fill
                exit_filled = False
                if pending_exit is not None:
                    if pending_exit.side > 0:
                        # BUY exit at bid: filled when bid drops below post price
                        if bid_p[i] < pending_exit.post_price:
                            exit_filled = True
                    else:
                        # SELL exit at ask: filled when ask rises above post price
                        if ask_p[i] > pending_exit.post_price:
                            exit_filled = True

                if exit_filled:
                    trade = self._build_trade(
                        direction    = position_side,
                        entry_idx    = position_entry_packet,
                        exit_idx     = i,
                        entry_ts     = position_entry_ts,
                        exit_ts      = ts[i],
                        entry_price  = position_entry_price,
                        exit_price   = pending_exit.post_price,
                        exit_method  = "maker_exit",
                    )
                    trades.append(trade)
                    pnl_by_packet[i] = trade.net_pnl
                    position_side    = 0
                    pending_exit     = None
                    last_exit_packet = i

                elif packets_held >= self.max_hold:
                    # Taker fallback exit
                    exit_price = self.costs.effective_taker_exit_price(mid[i], position_side)
                    trade = self._build_trade(
                        direction    = position_side,
                        entry_idx    = position_entry_packet,
                        exit_idx     = i,
                        entry_ts     = position_entry_ts,
                        exit_ts      = ts[i],
                        entry_price  = position_entry_price,
                        exit_price   = exit_price,
                        exit_method  = "taker_max_hold",
                    )
                    trades.append(trade)
                    pnl_by_packet[i] = trade.net_pnl
                    position_side    = 0
                    pending_exit     = None
                    last_exit_packet = i

            prev_abs_sig = abs_sig

        # End-of-day force close — taker
        if position_side != 0:
            i = n - 1
            exit_price = self.costs.effective_taker_exit_price(mid[i], position_side)
            trade = self._build_trade(
                direction    = position_side,
                entry_idx    = position_entry_packet,
                exit_idx     = i,
                entry_ts     = position_entry_ts,
                exit_ts      = ts[i],
                entry_price  = position_entry_price,
                exit_price   = exit_price,
                exit_method  = "taker_eod",
            )
            trades.append(trade)
            pnl_by_packet[i] += trade.net_pnl

        cum_pnl = pd.Series(pnl_by_packet, index=df["ts_ist"]).cumsum()
        return MakerBacktestResult(
            trades=trades, cumulative_pnl=cum_pnl,
            n_posts=n_posts, n_fills=n_fills, n_cancels=n_cancels,
        )

    def _build_trade(
        self,
        direction:    int,
        entry_idx:    int,
        exit_idx:     int,
        entry_ts:     object,
        exit_ts:      object,
        entry_price:  float,
        exit_price:   float,
        exit_method:  str,
    ) -> MakerTrade:
        qty       = self.lot_size * self.n_lots
        gross_pnl = direction * (exit_price - entry_price) * qty
        fee       = self.costs.fee_cost(
            entry_price, exit_price, self.lot_size, self.n_lots, direction
        )
        return MakerTrade(
            direction    = direction,
            entry_packet = entry_idx,
            exit_packet  = exit_idx,
            entry_ts     = pd.Timestamp(entry_ts),
            exit_ts      = pd.Timestamp(exit_ts),
            entry_price  = round(entry_price, 4),
            exit_price   = round(exit_price,  4),
            entry_method = "maker",
            exit_method  = exit_method,
            lot_size     = self.lot_size,
            n_lots       = self.n_lots,
            gross_pnl    = round(gross_pnl, 4),
            fee          = round(fee,       4),
            net_pnl      = round(gross_pnl - fee, 4),
            exit_reason  = exit_method,
        )
