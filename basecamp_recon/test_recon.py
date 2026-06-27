"""Tests for the Basecamp Recon regime/trend lens (pure math + load/run smoke)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from basecamp_recon.kalman import ConstantVelocityKalman, ema
from basecamp_recon.regime import variance_ratio, hurst_rs, classify_window, log_returns
from basecamp_recon.depth_load import load_depth
from basecamp_recon.analyze import run


# ── helpers ────────────────────────────────────────────────────────────────

def _ar1(n, phi, seed, sigma=1.0):
    rng = np.random.default_rng(seed)
    e = rng.normal(0, sigma, n)
    r = np.empty(n)
    r[0] = e[0]
    for i in range(1, n):
        r[i] = phi * r[i - 1] + e[i]
    return r


# ── Kalman ──────────────────────────────────────────────────────────────────

class TestKalman:
    def test_tracks_constant_velocity(self):
        n, v = 2000, 0.5
        rng = np.random.default_rng(0)
        price = 100 + v * np.arange(n) + rng.normal(0, 0.1, n)
        _, vel = ConstantVelocityKalman(q=1e-3, r=0.01).filter(price, np.ones(n))
        assert abs(np.mean(vel[-500:]) - v) < 0.1      # converges to true velocity

    def test_zero_velocity_on_flat(self):
        rng = np.random.default_rng(1)
        price = 100 + rng.normal(0, 0.1, 2000)
        _, vel = ConstantVelocityKalman(q=1e-4, r=0.01).filter(price)
        assert abs(np.mean(vel[-500:])) < 0.05         # no trend → ~0 velocity

    def test_level_tracks_price(self):
        rng = np.random.default_rng(2)
        price = 100 + np.cumsum(rng.normal(0, 0.05, 1000))
        lvl, _ = ConstantVelocityKalman(q=1e-3, r=0.01).filter(price)
        assert np.corrcoef(lvl[50:], price[50:])[0, 1] > 0.99

    def test_ema_smooths(self):
        x = np.array([0, 10, 0, 10, 0, 10], dtype=float)
        out = ema(x, 0.3)
        assert out.min() >= 0 and out.max() <= 10      # bounded, smoothed


# ── Regime diagnostics ────────────────────────────────────────────────────────

class TestRegime:
    def test_variance_ratio_detects_autocorrelation(self):
        assert variance_ratio(_ar1(5000, 0.0, 10), 10) == pytest.approx(1.0, abs=0.25)
        assert variance_ratio(_ar1(5000, 0.5, 11), 10) > 1.5     # momentum
        assert variance_ratio(_ar1(5000, -0.5, 12), 10) < 0.7    # mean-reversion

    def test_hurst_direction(self):
        rng = np.random.default_rng(5)
        n = 4000
        rw = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
        trend = 100 * np.exp(np.cumsum(0.0005 + rng.normal(0, 0.0002, n)))
        assert hurst_rs(trend) > hurst_rs(rw)          # persistent > random walk

    def test_classify_trend_up(self):
        rng = np.random.default_rng(7)
        price = 100 * np.exp(np.cumsum(0.0003 + rng.normal(0, 0.00005, 600)))
        assert classify_window(price)["regime"] == "trend_up"

    def test_classify_trend_down(self):
        rng = np.random.default_rng(8)
        price = 100 * np.exp(np.cumsum(-0.0003 + rng.normal(0, 0.00005, 600)))
        assert classify_window(price)["regime"] == "trend_down"

    def test_classify_mean_revert(self):
        # AR(1) on the level → price oscillates around 100, low drift, VR < 1
        price = 100 + _ar1(600, 0.7, 9, sigma=0.05)
        info = classify_window(price)
        assert info["regime"] == "mean_revert"
        assert info["vr"] < 0.7

    def test_classify_not_trend_on_random_walk(self):
        rng = np.random.default_rng(13)
        price = 100 * np.exp(np.cumsum(rng.normal(0, 0.0005, 600)))
        assert classify_window(price)["regime"] in ("chop", "mean_revert")


# ── Depth load ───────────────────────────────────────────────────────────────

class TestDepthLoad:
    def test_load_and_microprice(self, tmp_path):
        d, name = "2026-06-04", "HDFCBANK"
        p = tmp_path / d / name
        p.mkdir(parents=True)
        ts = pd.date_range("2026-06-04T04:00:00Z", periods=5, freq="1s")
        pd.DataFrame({
            "collector_received_at": ts,
            "bid_price_01": [100.0, 100.5, 0.0, 101.0, 101.5],   # row 2 = garbage
            "bid_qty_01": [500, 500, 500, 500, 500],
            "ask_price_01": [101.0, 101.5, 0.0, 102.0, 102.5],
            "ask_qty_01": [500, 500, 500, 500, 500],
        }).to_parquet(p / "compacted-x.parquet")

        out = load_depth(name, d, data_dir=str(tmp_path))
        assert len(out) == 4                                  # garbage dropped
        assert {"mid", "microprice", "micro_dev", "dt"} <= set(out.columns)
        assert out["mid"].iloc[0] == pytest.approx(100.5)
        assert out["microprice"].iloc[0] == pytest.approx(100.5)  # equal qty → mid

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_depth("NOPE", "2026-06-04", data_dir=str(tmp_path))


# ── Analyze runner ───────────────────────────────────────────────────────────

class TestAnalyze:
    def test_run_produces_velocity_and_windows(self, tmp_path):
        d, name, n = "2026-06-04", "SBIN", 1300
        p = tmp_path / d / name
        p.mkdir(parents=True)
        rng = np.random.default_rng(3)
        mid = 800 + np.cumsum(0.002 + rng.normal(0, 0.01, n))    # gentle uptrend
        pd.DataFrame({
            "collector_received_at": pd.date_range("2026-06-04T04:00:00Z", periods=n, freq="500ms"),
            "bid_price_01": mid - 0.5, "bid_qty_01": 500,
            "ask_price_01": mid + 0.5, "ask_qty_01": 500,
        }).to_parquet(p / "compacted.parquet")

        rdf, windows, summary = run(name, d, data_dir=str(tmp_path), window=600)
        assert len(rdf) == n and "kf_velocity" in rdf.columns
        assert summary["windows"] >= 2
        assert "regime_distribution" in summary and len(windows) == summary["windows"]
