"""Tests for the offline queue-aware fill haircut (Tier E)."""

from __future__ import annotations

import pandas as pd
import pytest

from paper_trader.broker import StrategyParams
from basecamp_recon.fill_sim import replay, haircut


def _book(rows: list[tuple]) -> pd.DataFrame:
    """rows = [(bid_p, bid_q, ask_p, ask_q), ...] → a depth DataFrame."""
    ts = pd.date_range("2026-06-18T04:00:00Z", periods=len(rows), freq="400ms")
    return pd.DataFrame({
        "collector_received_at": ts,
        "bid_price_01": [r[0] for r in rows],
        "bid_qty_01":   [r[1] for r in rows],
        "ask_price_01": [r[2] for r in rows],
        "ask_qty_01":   [r[3] for r in rows],
    })


def _long_then_touch(n_touch: int) -> pd.DataFrame:
    """Enter long @100, post exit @102, then n_touch packets that touch 102.1
    while the ask queue (1000) never clears."""
    rows = [(100.0, 990, 101.0, 10),       # post BUY @100
            (99.5, 500, 101.0, 10)]        # bid<100, consumes 490 → fill long
    rows += [(100.0, 500, 102.0, 1000)] * 4    # hold → exit posts @102.0, q=1000
    rows += [(100.0, 500, 102.1, 1000)] * n_touch   # touch, queue never clears
    return _book(rows)


# fast params so the realistic taker fallback resolves quickly
FAST = StrategyParams(min_hold_pkts=2, max_hold_packets=20)


class TestReplay:
    def test_optimistic_fills_maker_on_touch(self):
        out = replay(_long_then_touch(3), StrategyParams(**{**FAST.__dict__,
                     "queue_exit_fill": False}), underlying="HDFCBANK")
        assert out["n"] == 1
        assert out["trades"][0]["exit_method"] == "maker_exit"
        assert out["net"] > 0

    def test_queue_model_misses_maker_and_falls_to_taker(self):
        out = replay(_long_then_touch(30), StrategyParams(**{**FAST.__dict__,
                     "queue_exit_fill": True, "queue_exit_min_frac": 1.0}),
                     underlying="HDFCBANK")
        assert out["n"] == 1
        assert out["trades"][0]["exit_method"] in ("taker_max_hold", "taker_eod")
        assert out["maker_exit_rate"] == 0.0

    def test_realistic_net_not_above_optimistic(self):
        df = _long_then_touch(30)
        opt = replay(df, StrategyParams(**{**FAST.__dict__, "queue_exit_fill": False}),
                     underlying="HDFCBANK")
        real = replay(df, StrategyParams(**{**FAST.__dict__, "queue_exit_fill": True,
                      "queue_exit_min_frac": 1.0}), underlying="HDFCBANK")
        assert real["net"] <= opt["net"]          # the haircut is non-negative


class TestHaircut:
    def test_haircut_on_parquet(self, tmp_path):
        d, name = "2026-06-18", "HDFCBANK"
        p = tmp_path / d / name
        p.mkdir(parents=True)
        _long_then_touch(30).to_parquet(p / "compacted.parquet")
        h = haircut(name, d, data_dir=str(tmp_path), base=FAST, min_frac=1.0)
        assert h["optimistic_net"] >= h["realistic_net"]
        assert h["haircut_rupees"] >= 0
        assert h["optimistic_maker_rate"] >= h["realistic_maker_rate"]
        assert {"optimistic_by_exit", "realistic_by_exit"} <= set(h)
