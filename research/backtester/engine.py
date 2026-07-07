"""
Backtesting engine for NSE equity futures depth-feed signals.

Event-driven, packet-by-packet simulation with a full NSE cost model.
No third-party backtesting frameworks.

Slippage convention:
  Long  entry  - pay mid + N ticks  (crossing to ask side)
  Long  exit   - receive mid - N ticks (crossing to bid side)
  Short entry  - receive mid - N ticks (crossing to bid side)
  Short exit   - pay mid + N ticks  (crossing to ask side)

Exit conditions (evaluated in order):
  1. max_hold   - packets held >= max_hold
  2. stop       - unrealized PnL per share < -stop_ticks * TICK_SIZE
  3. reversal   - signal flips sign beyond reversal_threshold (if enabled)
  4. eod        - force-close any open position at the last packet
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

TICK_SIZE: float = 0.05


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------

@dataclass
class CostModel:
    """
    NSE equity futures fee schedule (discount broker - Zerodha/Dhan style).

    Brokerage is a flat per-order fee, not a percentage of notional.
    All percentage rates are as fractions of notional.
    GST (18%) applies to brokerage + exchange charges + SEBI fee.
    """
    brokerage_per_order: float = 20.0       # ₹ flat per order; 2 orders per round trip
    stt_sell:            float = 0.000125   # 0.0125% - sell side only
    exchange_charge:     float = 0.00002    # 0.002%  - both sides
    sebi_fee:            float = 0.000001   # 0.0001% - both sides
    stamp_duty_buy:      float = 0.00002    # 0.002%  - buy side only
    gst_rate:            float = 0.18       # 18% on brokerage + exchange + SEBI
    slippage_ticks:      int   = 1          # ticks of slippage per side

    def effective_price(self, midprice: float, direction: int, is_entry: bool) -> float:
        """
        Execution price after slippage.
        sign is positive when we are the aggressor paying more,
        negative when we are the aggressor receiving less.
        """
        sign = direction if is_entry else -direction
        return midprice + sign * self.slippage_ticks * TICK_SIZE

    def fee_cost(
        self,
        entry_price: float,
        exit_price:  float,
        lot_size:    float,
        n_lots:      int,
        direction:   int,
    ) -> float:
        """
        Total explicit fee charges for a round trip.
        Slippage is already captured in price difference - not repeated here.
        """
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
class Trade:
    direction:    int            # +1 long | -1 short
    entry_packet: int
    exit_packet:  int
    entry_ts:     pd.Timestamp
    exit_ts:      pd.Timestamp
    entry_price:  float          # effective execution price (includes slippage)
    exit_price:   float          # effective execution price (includes slippage)
    lot_size:     float
    n_lots:       int
    gross_pnl:    float          # direction * (exit_price - entry_price) * qty
    fee:          float          # explicit charges (brokerage, STT, etc.)
    net_pnl:      float          # gross_pnl - fee
    exit_reason:  str            # 'max_hold' | 'stop' | 'reversal' | 'eod'


@dataclass
class BacktestResult:
    trades:         list[Trade]
    cumulative_pnl: pd.Series    # net PnL cumulated per packet, indexed by ts_ist

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

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
    def max_drawdown(self) -> float:
        cum  = self.cumulative_pnl
        peak = cum.cummax()
        return float((cum - peak).min())

    def daily_pnl(self) -> pd.Series:
        """Net PnL summed by exit date. Use this for multi-day Sharpe calculation."""
        if not self.trades:
            return pd.Series(dtype=float)
        return (
            pd.DataFrame({
                "date":    [t.exit_ts.date() for t in self.trades],
                "net_pnl": [t.net_pnl        for t in self.trades],
            })
            .groupby("date")["net_pnl"]
            .sum()
        )

    def summary(self) -> dict:
        return {
            "n_trades":      self.n_trades,
            "net_pnl":       round(self.net_pnl, 2),
            "win_rate":      round(self.win_rate, 4) if self.trades else float("nan"),
            "profit_factor": round(self.profit_factor, 4),
            "max_drawdown":  round(self.max_drawdown, 2),
        }


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

class Backtester:
    """
    Packet-by-packet event-driven backtester for a single signed signal.

    Parameters
    ----------
    signal_col          : Column in the DataFrame used as the entry signal.
                          Positive → long, negative → short.
    entry_threshold     : Minimum |signal| to open a position.
    max_hold            : Maximum packets to hold before forced exit.
    stop_ticks          : Stop loss in ticks from effective entry price.
    min_hold            : Minimum packets held before reversal exit can trigger.
    reversal_threshold  : If set, exit early when signal flips beyond this level.
                          None disables reversal exit entirely.
    lot_size            : Shares per lot (instrument-specific; HDFCBANK = 550).
    n_lots              : Number of lots per trade.
    cost_model          : CostModel instance. Defaults to standard NSE rates.
    """

    def __init__(
        self,
        signal_col:         str,
        entry_threshold:    float,
        max_hold:           int            = 20,
        stop_ticks:         int            = 4,
        min_hold:           int            = 3,
        reversal_threshold: Optional[float] = None,
        cooldown:           int            = 0,
        fresh_cross:        bool           = False,
        lot_size:           float          = 550.0,
        n_lots:             int            = 1,
        cost_model:         Optional[CostModel] = None,
    ):
        self.signal_col         = signal_col
        self.entry_threshold    = entry_threshold
        self.max_hold           = max_hold
        self.stop_ticks         = stop_ticks
        self.min_hold           = min_hold
        self.reversal_threshold = reversal_threshold
        self.cooldown           = cooldown
        self.fresh_cross        = fresh_cross
        self.lot_size           = lot_size
        self.n_lots             = n_lots
        self.costs              = cost_model or CostModel()

    def run(self, df: pd.DataFrame) -> BacktestResult:
        """
        Run a single-session backtest.

        df must contain: ts_ist, midprice, and self.signal_col.
        Rows with NaN in signal or midprice are skipped.
        Any position still open at the last packet is force-closed (eod).
        """
        if self.signal_col not in df.columns:
            raise ValueError(f"signal_col '{self.signal_col}' not found in DataFrame")

        signal   = df[self.signal_col].to_numpy(float)
        midprice = df["midprice"].to_numpy(float)
        ts_arr   = df["ts_ist"].to_numpy()
        n        = len(df)

        trades:         list[Trade] = []
        pnl_by_packet:  np.ndarray  = np.zeros(n)

        in_position      = False
        direction        = 0
        entry_idx        = 0
        entry_price      = 0.0
        entry_ts_raw     = None
        last_exit_packet = -(self.cooldown + 1)  # allow entry from packet 0
        prev_abs_sig     = 0.0                   # for fresh_cross detection

        stop_distance = self.stop_ticks * TICK_SIZE

        for i in range(n):
            sig = signal[i]
            mid = midprice[i]

            if np.isnan(sig) or np.isnan(mid):
                prev_abs_sig = 0.0
                continue

            abs_sig = abs(sig)

            if not in_position:
                cooldown_ok  = (i - last_exit_packet) > self.cooldown
                threshold_ok = abs_sig >= self.entry_threshold
                cross_ok     = (not self.fresh_cross) or (prev_abs_sig < self.entry_threshold)

                if cooldown_ok and threshold_ok and cross_ok:
                    direction    = 1 if sig > 0 else -1
                    entry_price  = self.costs.effective_price(mid, direction, is_entry=True)
                    entry_idx    = i
                    entry_ts_raw = ts_arr[i]
                    in_position  = True

            else:
                packets_held        = i - entry_idx
                unrealized_per_unit = direction * (mid - entry_price)

                exit_reason: Optional[str] = None

                if packets_held >= self.max_hold:
                    exit_reason = "max_hold"
                elif unrealized_per_unit < -stop_distance:
                    exit_reason = "stop"
                elif (
                    self.reversal_threshold is not None
                    and packets_held >= self.min_hold
                    and direction * sig < -self.reversal_threshold
                ):
                    exit_reason = "reversal"

                if exit_reason is not None:
                    trade = self._close(
                        i, mid, ts_arr[i],
                        direction, entry_idx, entry_price, entry_ts_raw,
                        exit_reason,
                    )
                    trades.append(trade)
                    pnl_by_packet[i] = trade.net_pnl
                    last_exit_packet  = i
                    in_position       = False

            prev_abs_sig = abs_sig

        if in_position:
            i = n - 1
            trade = self._close(
                i, midprice[i], ts_arr[i],
                direction, entry_idx, entry_price, entry_ts_raw,
                "eod",
            )
            trades.append(trade)
            pnl_by_packet[i] += trade.net_pnl
            last_exit_packet = i

        cum_pnl = pd.Series(pnl_by_packet, index=df["ts_ist"]).cumsum()
        return BacktestResult(trades=trades, cumulative_pnl=cum_pnl)

    def _close(
        self,
        exit_idx:    int,
        exit_mid:    float,
        exit_ts_raw,
        direction:   int,
        entry_idx:   int,
        entry_price: float,
        entry_ts_raw,
        exit_reason: str,
    ) -> Trade:
        exit_price = self.costs.effective_price(exit_mid, direction, is_entry=False)
        qty        = self.lot_size * self.n_lots
        gross_pnl  = direction * (exit_price - entry_price) * qty
        fee        = self.costs.fee_cost(
            entry_price, exit_price, self.lot_size, self.n_lots, direction
        )
        return Trade(
            direction    = direction,
            entry_packet = entry_idx,
            exit_packet  = exit_idx,
            entry_ts     = pd.Timestamp(entry_ts_raw),
            exit_ts      = pd.Timestamp(exit_ts_raw),
            entry_price  = round(entry_price, 4),
            exit_price   = round(exit_price,  4),
            lot_size     = self.lot_size,
            n_lots       = self.n_lots,
            gross_pnl    = round(gross_pnl, 4),
            fee          = round(fee,        4),
            net_pnl      = round(gross_pnl - fee, 4),
            exit_reason  = exit_reason,
        )
