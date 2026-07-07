"""
Smoke tests for research/features/depth_features.py - composite signal.
"""

import numpy as np
import pandas as pd
import pytest

from research.features.depth_features import (
    add_all_features,
    add_composite,
    COMPOSITE_ZSCORE_WINDOW,
)

LEVELS = 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_depth_df(n: int = 500, seed: int = 42) -> pd.DataFrame:
    """
    Minimal synthetic DataFrame that mimics load_depth() output.
    Prices and quantities are random but structurally valid (bids < asks).
    """
    rng = np.random.default_rng(seed)
    ts  = pd.date_range("2026-05-06 09:20:00", periods=n, freq="400ms")

    mid  = 1650.0
    data = {"ts_ist": ts, "midprice": np.full(n, mid)}

    for i in range(1, LEVELS + 1):
        # Bids descend from mid, asks ascend from mid
        data[f"bid_price_{i:02d}"]  = mid - i * 0.05 + rng.normal(0, 0.01, n)
        data[f"ask_price_{i:02d}"]  = mid + i * 0.05 + rng.normal(0, 0.01, n)
        data[f"bid_qty_{i:02d}"]    = rng.integers(100, 5000, n)
        data[f"ask_qty_{i:02d}"]    = rng.integers(100, 5000, n)
        data[f"bid_orders_{i:02d}"] = rng.integers(1, 20, n)
        data[f"ask_orders_{i:02d}"] = rng.integers(1, 20, n)

    # obi is required by some downstream checks
    total_bid = sum(data[f"bid_qty_{i:02d}"] for i in range(1, LEVELS + 1))
    total_ask = sum(data[f"ask_qty_{i:02d}"] for i in range(1, LEVELS + 1))
    data["obi"] = (total_bid - total_ask) / (total_bid + total_ask + 1e-9)

    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# add_composite tests
# ---------------------------------------------------------------------------

class TestAddComposite:
    def test_composite_col_added(self):
        df  = _make_depth_df()
        df  = add_all_features(df, size_threshold=500, order_size=5000)
        assert "composite_eq" in df.columns

    def test_composite_not_all_nan(self):
        df  = _make_depth_df(n=500)
        df  = add_all_features(df, size_threshold=500, order_size=5000)
        valid = df["composite_eq"].dropna()
        assert len(valid) > 0, "composite_eq is entirely NaN"

    def test_composite_nan_only_in_warmup(self):
        # NaNs should only appear in the first ~zscore_window rows (warmup)
        df   = _make_depth_df(n=600)
        df   = add_all_features(df, size_threshold=500, order_size=5000)
        # After 2× the max window (shallowing=20, zscore=100), values must exist
        tail = df["composite_eq"].iloc[250:]
        assert tail.isna().sum() == 0, "unexpected NaNs after warmup period"

    def test_composite_scale_reasonable(self):
        # z-scored composite should stay within ~[-10, 10] for normal data
        df   = _make_depth_df(n=600)
        df   = add_all_features(df, size_threshold=500, order_size=5000)
        vals = df["composite_eq"].dropna()
        assert vals.abs().max() < 20, "composite has extreme outliers - z-score may be broken"

    def test_composite_has_variance(self):
        # Should not be a constant - must vary across packets
        df  = _make_depth_df(n=500)
        df  = add_all_features(df, size_threshold=500, order_size=5000)
        assert df["composite_eq"].std() > 0

    def test_composite_requires_a5_a6(self):
        # Calling add_composite without A5/A6 columns should raise ValueError
        df = _make_depth_df(n=200)
        with pytest.raises(ValueError, match="add_composite requires columns"):
            add_composite(df)

    def test_composite_symmetric_around_zero(self):
        # Mean of z-scored composite should be near zero
        df   = _make_depth_df(n=600)
        df   = add_all_features(df, size_threshold=500, order_size=5000)
        mean = df["composite_eq"].dropna().mean()
        assert abs(mean) < 0.5, f"composite mean {mean:.3f} is far from zero"


# ---------------------------------------------------------------------------
# add_all_features integration tests
# ---------------------------------------------------------------------------

class TestAddAllFeatures:
    def test_all_expected_cols_present(self):
        df       = _make_depth_df(n=500)
        df       = add_all_features(df, size_threshold=500, order_size=5000)
        expected = [
            "a1_gradient_asymmetry",
            "a2_frag_bid", "a2_frag_ask",
            "a3_symmetry_break",
            "a4_cog_divergence",
            "a5_condensation_signal",
            "a6_bid_shallowing", "a6_ask_shallowing",
            "composite_eq",
        ]
        for col in expected:
            assert col in df.columns, f"missing column: {col}"

    def test_row_count_unchanged(self):
        df  = _make_depth_df(n=300)
        n   = len(df)
        df2 = add_all_features(df, size_threshold=500, order_size=5000)
        assert len(df2) == n

    def test_zscore_window_param_respected(self):
        # Smaller zscore_window → fewer NaN rows in warmup
        df      = _make_depth_df(n=300)
        df_s    = add_all_features(df, size_threshold=500, order_size=5000, zscore_window=30)
        df_l    = add_all_features(df, size_threshold=500, order_size=5000, zscore_window=200)
        nan_s   = df_s["composite_eq"].isna().sum()
        nan_l   = df_l["composite_eq"].isna().sum()
        assert nan_s <= nan_l, "smaller window should produce fewer or equal NaN rows"
