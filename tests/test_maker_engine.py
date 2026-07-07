"""
Smoke tests for the maker backtester engine.
"""

import numpy as np
import pandas as pd
import pytest

from research.backtester.maker_engine import (
    MakerBacktester, MakerCostModel, MakerTrade, TICK_SIZE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flat_book(n: int = 200, mid: float = 100.0, half_spread_ticks: int = 1) -> pd.DataFrame:
    """Constant midprice with constant bid/ask. No price action."""
    half = half_spread_ticks * TICK_SIZE
    ts   = pd.date_range("2026-05-01 09:30:00", periods=n, freq="400ms")
    return pd.DataFrame({
        "ts_ist":       ts,
        "midprice":     np.full(n, mid),
        "bid_price_01": np.full(n, mid - half),
        "ask_price_01": np.full(n, mid + half),
        "signal":       np.zeros(n),
    })


def _stepping_book(
    n: int,
    mid_start: float,
    step_pkt:  int,
    step_size: float,
    half_spread_ticks: int = 1,
) -> pd.DataFrame:
    """Midprice steps down at packet `step_pkt` by `step_size`."""
    half = half_spread_ticks * TICK_SIZE
    mid  = np.full(n, mid_start)
    mid[step_pkt:] = mid_start + step_size
    ts = pd.date_range("2026-05-01 09:30:00", periods=n, freq="400ms")
    return pd.DataFrame({
        "ts_ist":       ts,
        "midprice":     mid,
        "bid_price_01": mid - half,
        "ask_price_01": mid + half,
        "signal":       np.zeros(n),
    })


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------

class TestMakerCostModel:
    def test_default_brokerage(self):
        m = MakerCostModel()
        assert m.brokerage_per_order == 20.0
        assert m.taker_slippage_ticks == 1

    def test_long_round_trip_fee_positive(self):
        m = MakerCostModel()
        fee = m.fee_cost(entry_price=100.0, exit_price=101.0,
                         lot_size=550.0, n_lots=1, direction=+1)
        # brokerage 40 + STT/exchange/etc on ~₹55K notional
        assert fee > 40
        assert fee < 100

    def test_taker_exit_long_pays_slippage(self):
        """Long exit sells at mid − slippage."""
        m = MakerCostModel(taker_slippage_ticks=1)
        price = m.effective_taker_exit_price(midprice=100.0, direction=+1)
        assert abs(price - (100.0 - TICK_SIZE)) < 1e-9

    def test_taker_exit_short_pays_slippage(self):
        """Short exit buys at mid + slippage."""
        m = MakerCostModel(taker_slippage_ticks=1)
        price = m.effective_taker_exit_price(midprice=100.0, direction=-1)
        assert abs(price - (100.0 + TICK_SIZE)) < 1e-9


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_invalid_exit_mode_raises(self):
        with pytest.raises(ValueError, match="exit_mode"):
            MakerBacktester("signal", entry_threshold=1.0, max_hold=10,
                            exit_mode="bogus")

    def test_missing_columns_raises(self):
        df = pd.DataFrame({"ts_ist": [pd.Timestamp.now()], "midprice": [100.0]})
        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=10)
        with pytest.raises(ValueError, match="requires columns"):
            bt.run(df)


# ---------------------------------------------------------------------------
# Entry logic
# ---------------------------------------------------------------------------

class TestMakerEntry:
    def test_no_entry_when_signal_below_threshold(self):
        df = _flat_book(n=200)
        df["signal"] = 0.5  # below threshold of 1.0
        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=10, order_timeout=5)
        res = bt.run(df)
        assert res.n_posts == 0
        assert res.n_trades == 0

    def test_entry_posted_on_strong_bullish_signal(self):
        df = _flat_book(n=200)
        df.loc[10:20, "signal"] = 2.0
        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=10, order_timeout=5)
        res = bt.run(df)
        assert res.n_posts > 0

    def test_entry_posted_on_strong_bearish_signal(self):
        df = _flat_book(n=200)
        df.loc[10:20, "signal"] = -2.0
        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=10, order_timeout=5)
        res = bt.run(df)
        assert res.n_posts > 0

    def test_orders_cancelled_on_timeout_in_flat_book(self):
        """A flat book never moves bid/ask - every posted order should time out."""
        df = _flat_book(n=200)
        df.loc[10:100, "signal"] = 2.0
        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=10, order_timeout=5)
        res = bt.run(df)
        assert res.n_posts > 0
        assert res.n_cancels > 0
        assert res.n_fills == 0


# ---------------------------------------------------------------------------
# Fill detection
# ---------------------------------------------------------------------------

class TestMakerFill:
    def test_buy_fills_when_bid_drops(self):
        """BUY posted at bid=99.95 fills when bid drops to 99.90 in a later packet."""
        # Use stepping book: mid drops by 1 tick at packet 15
        df = _stepping_book(n=100, mid_start=100.0, step_pkt=15,
                            step_size=-TICK_SIZE, half_spread_ticks=1)
        df.loc[5, "signal"] = 2.0  # post at packet 5 (bid=99.95)
        # At packet 15, mid → 99.95, so bid = 99.90 < 99.95 → fill
        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=20,
                             order_timeout=30, exit_mode="taker")
        res = bt.run(df)
        assert res.n_fills >= 1
        # Fill price should be the posted bid (99.95)
        assert any(abs(t.entry_price - 99.95) < 1e-6 for t in res.trades)

    def test_sell_fills_when_ask_rises(self):
        """SELL posted at ask=100.05 fills when ask rises to 100.10."""
        df = _stepping_book(n=100, mid_start=100.0, step_pkt=15,
                            step_size=+TICK_SIZE, half_spread_ticks=1)
        df.loc[5, "signal"] = -2.0
        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=20,
                             order_timeout=30, exit_mode="taker")
        res = bt.run(df)
        assert res.n_fills >= 1
        assert any(abs(t.entry_price - 100.05) < 1e-6 for t in res.trades)


# ---------------------------------------------------------------------------
# Exit logic
# ---------------------------------------------------------------------------

class TestMakerExit:
    def test_taker_exit_on_max_hold(self):
        """After filling, taker exit_mode should force-close at max_hold."""
        # Bid drops at packet 15 → entry fills. Then stays flat → max_hold exit.
        df = _stepping_book(n=100, mid_start=100.0, step_pkt=15,
                            step_size=-TICK_SIZE, half_spread_ticks=1)
        df.loc[5, "signal"] = 2.0
        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=10,
                             order_timeout=30, exit_mode="taker")
        res = bt.run(df)
        assert res.n_trades >= 1
        # All exits must be taker (max_hold or eod) in taker mode
        for t in res.trades:
            assert "taker" in t.exit_method

    def test_eod_force_close(self):
        """Position still open at last packet must be force-closed via taker."""
        # Long fill near the end with hold longer than remaining packets
        df = _stepping_book(n=50, mid_start=100.0, step_pkt=40,
                            step_size=-TICK_SIZE, half_spread_ticks=1)
        df.loc[35, "signal"] = 2.0
        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=1000,
                             order_timeout=10, exit_mode="taker")
        res = bt.run(df)
        if res.n_fills > 0:
            assert any(t.exit_method == "taker_eod" for t in res.trades)


# ---------------------------------------------------------------------------
# PnL accounting
# ---------------------------------------------------------------------------

class TestPnL:
    def test_winning_long_in_taker_mode(self):
        """
        Long: filled at bid=99.95 (when bid drops), exits via taker at later
        higher mid. Should be profitable gross.
        """
        n = 100
        mid = np.full(n, 100.0)
        mid[15:] = 99.95   # bid drops at 15 → entry fills at 99.95
        mid[30:] = 100.50  # then price rises significantly
        ts = pd.date_range("2026-05-01 09:30:00", periods=n, freq="400ms")
        df = pd.DataFrame({
            "ts_ist":       ts,
            "midprice":     mid,
            "bid_price_01": mid - TICK_SIZE,
            "ask_price_01": mid + TICK_SIZE,
            "signal":       np.zeros(n),
        })
        df.loc[5, "signal"] = 2.0

        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=20,
                             order_timeout=30, exit_mode="taker")
        res = bt.run(df)
        assert res.n_trades >= 1
        t = res.trades[0]
        assert t.direction == 1
        # Entry at posted bid (99.95). Exit at mid - 1tick after max_hold.
        # Position held from packet ~15 to ~35. Mid at 35 ≈ 100.50, exit ≈ 100.45.
        # Gross = (100.45 - 99.95) × 550 = 275
        assert t.gross_pnl > 0
        assert t.net_pnl == round(t.gross_pnl - t.fee, 4)

    def test_long_entry_price_equals_posted_bid(self):
        df = _stepping_book(n=100, mid_start=100.0, step_pkt=15,
                            step_size=-TICK_SIZE, half_spread_ticks=1)
        df.loc[5, "signal"] = 2.0
        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=20,
                             order_timeout=30, exit_mode="taker", lot_size=550.0)
        res = bt.run(df)
        assert res.n_fills >= 1
        assert abs(res.trades[0].entry_price - 99.95) < 1e-6


# ---------------------------------------------------------------------------
# Cooldown / fresh-cross
# ---------------------------------------------------------------------------

class TestEntryFiltering:
    def test_cooldown_prevents_immediate_repost(self):
        """After an exit, no new orders for `cooldown` packets."""
        df = _stepping_book(n=200, mid_start=100.0, step_pkt=15,
                            step_size=-TICK_SIZE, half_spread_ticks=1)
        df["signal"] = 2.0  # sustained signal
        # Long cooldown should heavily reduce post count
        bt_short = MakerBacktester("signal", entry_threshold=1.0, max_hold=5,
                                   order_timeout=5, exit_mode="taker", cooldown=0)
        bt_long  = MakerBacktester("signal", entry_threshold=1.0, max_hold=5,
                                   order_timeout=5, exit_mode="taker", cooldown=50)
        r_short = bt_short.run(df)
        r_long  = bt_long.run(df)
        assert r_long.n_posts <= r_short.n_posts

    def test_fresh_cross_avoids_repost_while_signal_high(self):
        """If signal is constant above threshold, fresh_cross posts ONE order."""
        df = _flat_book(n=200)
        df["signal"] = 2.0  # sustained - never crosses fresh
        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=5,
                             order_timeout=5, exit_mode="taker", fresh_cross=True)
        res = bt.run(df)
        # First packet: prev_abs_sig=0 < 1.0, abs_sig=2.0 ≥ 1.0 → one post.
        # After cancel, prev_abs_sig stays at 2.0, fresh_cross blocks further posts.
        assert res.n_posts == 1


# ---------------------------------------------------------------------------
# Result diagnostics
# ---------------------------------------------------------------------------

class TestResultDiagnostics:
    def test_summary_has_required_keys(self):
        df = _flat_book(n=100)
        df.loc[10:50, "signal"] = 2.0
        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=10,
                             order_timeout=5)
        res = bt.run(df)
        s = res.summary()
        for k in ["n_posts", "n_fills", "fill_rate", "n_trades",
                  "net_pnl", "gross_pnl", "total_fees", "max_drawdown"]:
            assert k in s

    def test_exit_method_counts_sums_to_trades(self):
        df = _stepping_book(n=200, mid_start=100.0, step_pkt=15,
                            step_size=-TICK_SIZE, half_spread_ticks=1)
        df.loc[5, "signal"] = 2.0
        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=10,
                             order_timeout=30, exit_mode="taker")
        res = bt.run(df)
        counts = res.exit_method_counts()
        assert sum(counts.values()) == res.n_trades

    def test_empty_result_metrics_are_nan(self):
        df = _flat_book(n=100)  # signal stays 0 → no trades
        bt = MakerBacktester("signal", entry_threshold=1.0, max_hold=10)
        res = bt.run(df)
        assert res.n_trades == 0
        assert np.isnan(res.win_rate)
        assert np.isnan(res.maker_exit_rate)
