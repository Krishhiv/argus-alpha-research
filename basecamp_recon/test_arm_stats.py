"""Tests for the Tier-A arm-evaluation statistics (pure-math properties)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from basecamp_recon.arm_stats import (
    _norm_cdf, _norm_ppf, sharpe, autocorr, n_eff, psr, expected_max_sharpe,
    deflated_sharpe, pbo_cscv, paired_diff, block_bootstrap_sharpe_ci,
    attribution_by_instrument, attribution_by_exit,
)


# ── normal helpers ──────────────────────────────────────────────────────────────

class TestNormal:
    def test_cdf_known_points(self):
        assert _norm_cdf(0.0) == pytest.approx(0.5, abs=1e-9)
        assert _norm_cdf(1.959963985) == pytest.approx(0.975, abs=1e-6)

    def test_ppf_inverse_of_cdf(self):
        for p in (0.01, 0.25, 0.5, 0.84, 0.99):
            assert _norm_cdf(_norm_ppf(p)) == pytest.approx(p, abs=1e-6)


# ── sharpe / autocorr / n_eff ────────────────────────────────────────────────────

class TestBasics:
    def test_sharpe_sign_and_scale(self):
        r = np.array([1.0, 2.0, 3.0, 4.0])
        assert sharpe(r) > 0
        assert sharpe(r) == pytest.approx(sharpe(r * 100))      # scale-free

    def test_sharpe_zero_var(self):
        assert sharpe(np.ones(5)) == 0.0

    def test_autocorr_positive_for_trending(self):
        x = np.cumsum(np.ones(50))                              # strongly persistent
        assert autocorr(x, 1) > 0.5

    def test_n_eff_shrinks_under_autocorrelation(self):
        rng = np.random.default_rng(0)
        n = 4000
        # AR(1) phi=0.8 → effective sample far below n
        e = rng.normal(size=n)
        x = np.empty(n); x[0] = e[0]
        for i in range(1, n):
            x[i] = 0.8 * x[i - 1] + e[i]
        ne = n_eff(x)
        assert ne < n / 3
        # iid → n_eff ≈ n
        assert n_eff(rng.normal(size=n)) > 0.7 * n

    def test_n_eff_iid_close_to_n(self):
        rng = np.random.default_rng(1)
        x = rng.normal(size=2000)
        assert n_eff(x) == pytest.approx(2000, rel=0.4)


# ── PSR / DSR ─────────────────────────────────────────────────────────────────

class TestPSR:
    def test_psr_monotone_in_n(self):
        # more observations → more confident the Sharpe beats 0
        p_small = psr(0.5, n=10, skew=0.0, kurt=3.0)
        p_large = psr(0.5, n=200, skew=0.0, kurt=3.0)
        assert 0.5 < p_small < p_large < 1.0

    def test_psr_zero_sharpe_is_half(self):
        assert psr(0.0, n=50, skew=0.0, kurt=3.0) == pytest.approx(0.5, abs=1e-9)

    def test_negative_skew_hurts_psr(self):
        base = psr(0.5, n=100, skew=0.0, kurt=3.0)
        neg = psr(0.5, n=100, skew=-1.0, kurt=6.0)             # left tail + fat tail
        assert neg < base

    def test_expected_max_sharpe_grows_with_trials(self):
        assert expected_max_sharpe(0.3, 2) < expected_max_sharpe(0.3, 50)
        assert expected_max_sharpe(0.0, 50) == 0.0            # no dispersion → no inflation

    def test_dsr_below_psr_due_to_deflation(self):
        # one arm's Sharpe, judged against a spread of trial Sharpes
        sr_trials = np.array([0.1, 0.2, 0.3, 0.5, 0.4, -0.1, 0.0])
        plain = psr(0.5, n=12, skew=0.0, kurt=3.0)
        dsr, sr_star = deflated_sharpe(0.5, 12, 0.0, 3.0, sr_trials)
        assert sr_star > 0
        assert dsr < plain                                     # deflation lowers confidence


# ── PBO / CSCV ───────────────────────────────────────────────────────────────────

class TestPBO:
    def test_pbo_low_for_genuinely_best_arm(self):
        # arm A truly dominates every day → IS-best is always OOS-best → PBO ≈ 0
        rng = np.random.default_rng(3)
        T = 12
        M = pd.DataFrame({
            "A": 5.0 + rng.normal(0, 0.5, T),
            "B": rng.normal(0, 1, T),
            "C": rng.normal(0, 1, T),
            "D": rng.normal(0, 1, T),
        })
        assert pbo_cscv(M, s=6)["pbo"] < 0.1

    def test_pbo_high_for_noise(self):
        # all arms iid noise → IS winner is luck → OOS rank random → PBO ≈ 0.5
        rng = np.random.default_rng(4)
        M = pd.DataFrame(rng.normal(0, 1, (12, 6)),
                         columns=list("ABCDEF"))
        assert pbo_cscv(M, s=6)["pbo"] > 0.3


# ── paired diff / bootstrap ──────────────────────────────────────────────────────

class TestPaired:
    def test_paired_diff_detects_constant_edge(self):
        a = pd.Series([10, 11, 9, 12, 8], dtype=float)
        b = a - 3.0                                            # a beats b by 3 every day
        res = paired_diff(a, b, n_boot=2000, seed=0)
        assert res["mean"] == pytest.approx(3.0)
        assert res["p_gt0"] > 0.99
        assert res["ci95"][0] > 0

    def test_paired_diff_no_edge_straddles_zero(self):
        rng = np.random.default_rng(6)
        a = pd.Series(rng.normal(0, 1, 12))
        b = pd.Series(rng.normal(0, 1, 12))
        res = paired_diff(a, b, n_boot=2000, seed=1)
        lo, hi = res["ci95"]
        assert lo < 0 < hi

    def test_block_bootstrap_ci_brackets_point(self):
        rng = np.random.default_rng(7)
        r = rng.normal(1.0, 1.0, 60)
        lo, hi = block_bootstrap_sharpe_ci(r, block=2, n_boot=2000)
        assert lo < sharpe(r) < hi


# ── attribution ──────────────────────────────────────────────────────────────────

class TestAttribution:
    def _trades(self):
        return pd.DataFrame({
            "underlying": ["A", "A", "B", "B"],
            "exit_method": ["maker", "taker_stop", "maker", "taker_stop"],
            "net_pnl": [100.0, -50.0, 30.0, -10.0],
        })

    def test_by_instrument_sums_and_sorts(self):
        out = attribution_by_instrument(self._trades())
        assert out.loc["A", "net"] == 50.0
        assert out.loc["B", "net"] == 20.0
        assert list(out.index) == ["A", "B"]                  # sorted by net desc

    def test_by_exit_groups(self):
        out = attribution_by_exit(self._trades())
        assert out.loc["maker", "net"] == 130.0
        assert out.loc["taker_stop", "net"] == -60.0
