"""Tests for markout/adverse-selection logic using synthetic depth + trades."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import basecamp_recon.markout as mk


def _write_depth(tmp_path, name, date, mids, start="2026-06-24T04:00:00Z", freq_s=1.0):
    """One compacted parquet for a symbol-day with a given mid path (spread=1.0)."""
    d = tmp_path / f"trading_date={date}" / f"symbol={name}-Jun2026-FUT"
    d.mkdir(parents=True)
    ts = pd.date_range(start, periods=len(mids), freq=f"{freq_s}s")
    mids = np.asarray(mids, float)
    pd.DataFrame({
        "collector_received_at": ts,
        "bid_price_01": mids - 0.5,
        "ask_price_01": mids + 0.5,
    }).to_parquet(d / f"compacted-depth-{date}-{name}.parquet")


def _trades(rows):
    cols = ["underlying", "date", "direction", "entry_ts", "exit_ts",
            "entry_price", "exit_price", "lot_size", "n_lots", "net_pnl", "fee"]
    return pd.DataFrame(rows, columns=cols)


def test_finds_compacted_file(tmp_path):
    _write_depth(tmp_path, "ICICIBANK", "2026-06-24", [100, 101, 102])
    f = mk.find_depth_file("ICICIBANK", "2026-06-24", str(tmp_path))
    assert f and "compacted" in f


def test_load_day_mid_cleans_and_sorts(tmp_path):
    _write_depth(tmp_path, "SBIN", "2026-06-24", [100, 100.5, 101])
    d = mk.load_day_mid("SBIN", "2026-06-24", str(tmp_path))
    assert list(d.mid) == [100.0, 100.5, 101.0]
    assert d.ts.is_monotonic_increasing


def test_markout_captures_favorable_fill_and_adverse_drift(tmp_path, monkeypatch):
    # Mid rises 100→100 for 5s then FALLS - a long that filled cheap then gets run over.
    # mids per second: t0=100, t1..: stays then declines after fill
    mids = [100.0, 100.0, 100.0, 100.0, 100.0, 99.0, 98.0]  # falls after 5s
    _write_depth(tmp_path, "AXISBANK", "2026-06-24", mids)
    # Long entry at the bid (=mid-0.5=99.5) at t0; exits later.
    tr = _trades([["AXISBANK", "2026-06-24", 1,
                   "2026-06-24T04:00:00Z", "2026-06-24T04:00:06Z",
                   99.5, 100.0, 100, 1, 50.0, 0.0]])
    monkeypatch.setattr(mk, "CLEAN_DAYS", ["2026-06-24"])
    p = tmp_path / "t.csv"; tr.to_csv(p, index=False)
    df, summ = mk.compute(str(p), str(tmp_path))
    # h=0: mid(100) - entry(99.5) = +0.5  (captured half-spread, favorable)
    assert summ["markout_per_share"]["0s"] == pytest.approx(0.5, abs=1e-6)
    # h=5: mid fell to 99 → 99 - 99.5 = -0.5 (adverse selection erased the edge)
    assert summ["markout_per_share"]["5s"] < 0


def test_mid_to_mid_strips_spread_capture(tmp_path, monkeypatch):
    # Flat market (mid constant=100). A "winning" sim trade that booked +profit from
    # favorable fills should show ~0 mid-to-mid (no real directional move).
    _write_depth(tmp_path, "RELIANCE", "2026-06-24", [100.0] * 60, freq_s=1.0)
    tr = _trades([["RELIANCE", "2026-06-24", -1,
                   "2026-06-24T04:00:02Z", "2026-06-24T04:00:20Z",
                   100.5, 99.5, 100, 1, 99.0, 1.0]])  # sim booked +99 from spread
    monkeypatch.setattr(mk, "CLEAN_DAYS", ["2026-06-24"])
    p = tmp_path / "t.csv"; tr.to_csv(p, index=False)
    df, summ = mk.compute(str(p), str(tmp_path))
    # mid_entry == mid_exit == 100 → mid-to-mid = -fee, i.e. ≤ 0, far below sim_net
    assert summ["mid_to_mid_total"] < summ["sim_net_total"]
    assert summ["mid_to_mid_total"] == pytest.approx(-1.0, abs=1e-6)


def test_skips_missing_symbol_days(tmp_path, monkeypatch):
    _write_depth(tmp_path, "ICICIBANK", "2026-06-24", [100, 101])
    tr = _trades([
        ["ICICIBANK", "2026-06-24", 1, "2026-06-24T04:00:00Z", "2026-06-24T04:00:01Z", 99.5, 100.5, 100, 1, 50.0, 0.0],
        ["SBIN",      "2026-06-24", 1, "2026-06-24T04:00:00Z", "2026-06-24T04:00:01Z", 99.5, 100.5, 100, 1, 50.0, 0.0],  # no depth
    ])
    monkeypatch.setattr(mk, "CLEAN_DAYS", ["2026-06-24"])
    p = tmp_path / "t.csv"; tr.to_csv(p, index=False)
    df, summ = mk.compute(str(p), str(tmp_path))
    assert summ["n_trades"] == 1                       # SBIN dropped
    assert "SBIN 2026-06-24" in summ["skipped_symbol_days"]
