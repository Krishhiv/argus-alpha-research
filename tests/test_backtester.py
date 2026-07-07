"""
Smoke tests for research/backtester/engine.py.

All tests use synthetic DataFrames - no parquet files required.
"""

import numpy as np
import pandas as pd
import pytest

from research.backtester.engine import (
    TICK_SIZE,
    Backtester,
    BacktestResult,
    CostModel,
    Trade,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(
    n:        int   = 100,
    mid:      float = 1650.0,
    signal:   float = 1.0,
    mid_step: float = 0.0,
) -> pd.DataFrame:
    """
    Synthetic single-day DataFrame.
    mid_step > 0 → rising price each packet.
    mid_step < 0 → falling price each packet.
    """
    ts       = pd.date_range("2026-05-06 09:20:00", periods=n, freq="400ms")
    midprice = mid + np.arange(n) * mid_step
    return pd.DataFrame({
        "ts_ist":      ts,
        "midprice":    midprice,
        "sig":         np.full(n, signal),
    })


def _default_bt(**kwargs) -> Backtester:
    return Backtester(signal_col="sig", entry_threshold=0.5, **kwargs)


# ---------------------------------------------------------------------------
# CostModel tests
# ---------------------------------------------------------------------------

class TestCostModel:
    def test_fee_cost_long_is_positive(self):
        cm  = CostModel()
        fee = cm.fee_cost(1650.0, 1651.0, 550.0, 1, direction=1)
        assert fee > 0

    def test_fee_cost_short_is_positive(self):
        cm  = CostModel()
        fee = cm.fee_cost(1650.0, 1649.0, 550.0, 1, direction=-1)
        assert fee > 0

    def test_long_entry_price_above_mid(self):
        cm  = CostModel(slippage_ticks=1)
        ep  = cm.effective_price(1650.0, direction=1, is_entry=True)
        assert ep == pytest.approx(1650.0 + TICK_SIZE)

    def test_long_exit_price_below_mid(self):
        cm  = CostModel(slippage_ticks=1)
        xp  = cm.effective_price(1650.0, direction=1, is_entry=False)
        assert xp == pytest.approx(1650.0 - TICK_SIZE)

    def test_short_entry_price_below_mid(self):
        cm  = CostModel(slippage_ticks=1)
        ep  = cm.effective_price(1650.0, direction=-1, is_entry=True)
        assert ep == pytest.approx(1650.0 - TICK_SIZE)

    def test_short_exit_price_above_mid(self):
        cm  = CostModel(slippage_ticks=1)
        xp  = cm.effective_price(1650.0, direction=-1, is_entry=False)
        assert xp == pytest.approx(1650.0 + TICK_SIZE)

    def test_zero_slippage_prices_equal_mid(self):
        cm = CostModel(slippage_ticks=0)
        assert cm.effective_price(1650.0, 1, True)  == pytest.approx(1650.0)
        assert cm.effective_price(1650.0, 1, False) == pytest.approx(1650.0)


# ---------------------------------------------------------------------------
# Entry tests
# ---------------------------------------------------------------------------

class TestEntry:
    def test_no_trades_when_signal_below_threshold(self):
        df  = _make_df(signal=0.3)
        bt  = _default_bt()            # entry_threshold=0.5
        res = bt.run(df)
        assert res.n_trades == 0

    def test_long_trade_fires_on_positive_signal(self):
        df  = _make_df(signal=1.0)
        bt  = _default_bt(max_hold=10)
        res = bt.run(df)
        assert res.n_trades >= 1
        assert res.trades[0].direction == 1

    def test_short_trade_fires_on_negative_signal(self):
        df  = _make_df(signal=-1.0)
        bt  = _default_bt(max_hold=10)
        res = bt.run(df)
        assert res.n_trades >= 1
        assert res.trades[0].direction == -1

    def test_missing_signal_col_raises(self):
        df = _make_df()
        bt = Backtester(signal_col="nonexistent", entry_threshold=0.5)
        with pytest.raises(ValueError, match="nonexistent"):
            bt.run(df)

    def test_all_nan_signal_no_trades(self):
        df          = _make_df()
        df["sig"]   = np.nan
        bt          = _default_bt()
        res         = bt.run(df)
        assert res.n_trades == 0


# ---------------------------------------------------------------------------
# Exit reason tests
# ---------------------------------------------------------------------------

class TestExitReasons:
    def test_max_hold_exit(self):
        # Signal is always on, so after each max_hold exit a new position opens
        # immediately. The last position may be force-closed at eod instead.
        # Assert the first trade exits at max_hold.
        df  = _make_df(n=50, signal=1.0, mid_step=0.0)
        bt  = _default_bt(max_hold=10, stop_ticks=999)
        res = bt.run(df)
        assert res.n_trades >= 1
        assert res.trades[0].exit_reason == "max_hold"

    def test_stop_loss_exit(self):
        # Price falls 6 ticks per packet → stop at stop_ticks=4 should trigger fast
        df  = _make_df(n=50, signal=1.0, mid_step=-6 * TICK_SIZE)
        bt  = _default_bt(max_hold=100, stop_ticks=4)
        res = bt.run(df)
        assert res.n_trades >= 1
        assert res.trades[0].exit_reason == "stop"

    def test_reversal_exit(self):
        n   = 50
        sig = np.full(n, 1.0)
        sig[10:] = -2.0          # strong reversal after 10 packets
        df  = pd.DataFrame({
            "ts_ist":   pd.date_range("2026-05-06 09:20:00", periods=n, freq="400ms"),
            "midprice": np.full(n, 1650.0),
            "sig":      sig,
        })
        bt  = _default_bt(max_hold=100, stop_ticks=999, min_hold=3, reversal_threshold=0.5)
        res = bt.run(df)
        assert res.n_trades >= 1
        assert res.trades[0].exit_reason == "reversal"

    def test_eod_force_close(self):
        # max_hold larger than the entire DataFrame → must close at eod
        df  = _make_df(n=30, signal=1.0, mid_step=0.0)
        bt  = _default_bt(max_hold=10_000, stop_ticks=999)
        res = bt.run(df)
        assert res.n_trades >= 1
        assert res.trades[-1].exit_reason == "eod"


# ---------------------------------------------------------------------------
# PnL correctness tests
# ---------------------------------------------------------------------------

class TestPnL:
    def test_net_pnl_less_than_gross(self):
        df  = _make_df(n=50, signal=1.0, mid_step=TICK_SIZE)
        bt  = _default_bt(max_hold=20, stop_ticks=999)
        res = bt.run(df)
        for t in res.trades:
            assert t.net_pnl < t.gross_pnl, "fees must reduce PnL"

    def test_fee_always_positive(self):
        df  = _make_df(n=50, signal=1.0, mid_step=TICK_SIZE)
        bt  = _default_bt(max_hold=20)
        res = bt.run(df)
        for t in res.trades:
            assert t.fee > 0

    def test_winning_long_trade(self):
        # Price rises 1 tick per packet over 20 packets → 20-tick gross gain.
        # After realistic costs (~₹225 on 1 lot HDFCBANK) net PnL should be positive.
        df  = _make_df(n=50, signal=1.0, mid_step=TICK_SIZE)
        bt  = _default_bt(max_hold=20, stop_ticks=999)
        res = bt.run(df)
        assert res.trades[0].gross_pnl > 0
        assert res.trades[0].net_pnl   > 0

    def test_winning_short_trade(self):
        # Price falls 1 tick per packet. Short should profit after costs.
        df  = _make_df(n=50, signal=-1.0, mid_step=-TICK_SIZE)
        bt  = _default_bt(max_hold=20, stop_ticks=999)
        res = bt.run(df)
        assert res.trades[0].gross_pnl > 0
        assert res.trades[0].net_pnl   > 0

    def test_flat_price_round_trip_loses_money(self):
        # No price movement → round-trip costs and slippage eat into PnL.
        df  = _make_df(n=50, signal=1.0, mid_step=0.0)
        bt  = _default_bt(max_hold=10, stop_ticks=999)
        res = bt.run(df)
        assert res.trades[0].net_pnl < 0

    def test_cumulative_pnl_length_matches_df(self):
        df  = _make_df(n=100, signal=1.0, mid_step=TICK_SIZE)
        bt  = _default_bt(max_hold=20)
        res = bt.run(df)
        assert len(res.cumulative_pnl) == len(df)

    def test_cumulative_pnl_is_monotone_between_trades(self):
        # Between trade exits the cumulative PnL should be flat (no change)
        df  = _make_df(n=100, signal=1.0, mid_step=TICK_SIZE)
        bt  = _default_bt(max_hold=20)
        res = bt.run(df)
        diffs = res.cumulative_pnl.diff().dropna()
        # Most packets have zero PnL change (only exit packets are non-zero)
        assert (diffs == 0).sum() > len(df) // 2


# ---------------------------------------------------------------------------
# Metrics / result tests
# ---------------------------------------------------------------------------

class TestBacktestResult:
    def _run(self, **kwargs) -> BacktestResult:
        df = _make_df(n=100, signal=1.0, mid_step=TICK_SIZE)
        bt = _default_bt(max_hold=20, stop_ticks=999, **kwargs)
        return bt.run(df)

    def test_summary_has_required_keys(self):
        res  = self._run()
        keys = res.summary().keys()
        for k in ("n_trades", "net_pnl", "win_rate", "profit_factor", "max_drawdown"):
            assert k in keys

    def test_win_rate_between_0_and_1(self):
        res = self._run()
        assert 0.0 <= res.win_rate <= 1.0

    def test_profit_factor_positive(self):
        res = self._run()
        assert res.profit_factor >= 0

    def test_no_trades_result(self):
        df  = _make_df(signal=0.1)   # below threshold
        bt  = _default_bt()
        res = bt.run(df)
        assert res.n_trades == 0
        assert res.net_pnl  == 0.0
        assert np.isnan(res.win_rate)

    def test_daily_pnl_returns_series(self):
        res = self._run()
        dp  = res.daily_pnl()
        assert isinstance(dp, pd.Series)
        assert len(dp) >= 1
