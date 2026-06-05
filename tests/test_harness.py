"""Tests for the multi-arm harness: runtime build, universe pruning, fan-out, isolation."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from paper_trader.arms import Arm, all_universe_symbols, ARMS
from paper_trader.broker import StrategyParams
from paper_trader.contracts import ResolvedContract
from paper_trader.harness import build_runtimes, dispatch, build_combined

UTC = timezone.utc
TS = datetime(2026, 6, 8, 4, 0, tzinfo=UTC)


def _contracts(symbols, lot=550):
    return {s: ResolvedContract(s, 60000 + i, lot, date(2026, 6, 30)) for i, s in enumerate(symbols)}


def _arms():
    champ = StrategyParams()
    return [
        Arm("a", ["HDFCBANK", "ICICIBANK"], champ, "two names"),
        Arm("b", ["HDFCBANK"], StrategyParams(stop_loss_ticks=0), "one name, no stop"),
    ]


class TestBuildRuntimes:
    def test_one_runtime_per_arm_with_own_universe(self, tmp_path: Path):
        rts = build_runtimes(_contracts(["HDFCBANK", "ICICIBANK"]), arms=_arms(), logs_dir=str(tmp_path))
        assert [rt.arm.name for rt in rts] == ["a", "b"]
        assert set(rts[0].brokers) == {"HDFCBANK", "ICICIBANK"}
        assert set(rts[1].brokers) == {"HDFCBANK"}

    def test_independent_risk_governors(self, tmp_path: Path):
        rts = build_runtimes(_contracts(["HDFCBANK", "ICICIBANK"]), arms=_arms(), logs_dir=str(tmp_path))
        assert rts[0].risk is not rts[1].risk

    def test_params_and_lot_applied(self, tmp_path: Path):
        rts = build_runtimes(_contracts(["HDFCBANK", "ICICIBANK"], lot=999), arms=_arms(), logs_dir=str(tmp_path))
        assert rts[1].brokers["HDFCBANK"].p.stop_loss_ticks == 0   # arm b's param
        assert rts[0].brokers["HDFCBANK"].lot_size == 999          # injected lot size

    def test_prunes_unresolvable_symbols(self, tmp_path: Path):
        # ICICIBANK missing from contracts → arm 'a' keeps only HDFCBANK
        rts = build_runtimes(_contracts(["HDFCBANK"]), arms=_arms(), logs_dir=str(tmp_path))
        a = next(rt for rt in rts if rt.arm.name == "a")
        assert set(a.brokers) == {"HDFCBANK"}

    def test_skips_arm_with_no_resolvable_instruments(self, tmp_path: Path):
        rts = build_runtimes(_contracts(["RELIANCE"]), arms=_arms(), logs_dir=str(tmp_path))
        assert rts == []   # neither arm trades RELIANCE


class TestFanOut:
    def test_dispatch_reaches_all_arms_for_symbol(self, tmp_path: Path):
        rts = build_runtimes(_contracts(["HDFCBANK", "ICICIBANK"]), arms=_arms(), logs_dir=str(tmp_path))
        # one HDFCBANK packet → both arms (a and b trade HDFCBANK) advance a packet
        dispatch(rts, "HDFCBANK", ts_utc=TS, bid_price=743.0, bid_qty=990, ask_price=744.0, ask_qty=10)
        assert rts[0].brokers["HDFCBANK"]._packet_idx == 1
        assert rts[1].brokers["HDFCBANK"]._packet_idx == 1
        # ICICIBANK only in arm a
        dispatch(rts, "ICICIBANK", ts_utc=TS, bid_price=1450.0, bid_qty=990, ask_price=1451.0, ask_qty=10)
        assert rts[0].brokers["ICICIBANK"]._packet_idx == 1

    def test_dispatch_ignores_unknown_symbol(self, tmp_path: Path):
        rts = build_runtimes(_contracts(["HDFCBANK"]), arms=_arms(), logs_dir=str(tmp_path))
        dispatch(rts, "TCS", ts_utc=TS, bid_price=2460.0, bid_qty=10, ask_price=2461.0, ask_qty=10)  # no-op


class TestBuildCombined:
    def test_combined_snapshot_shape(self, tmp_path: Path):
        rts = build_runtimes(_contracts(["HDFCBANK", "ICICIBANK"]), arms=_arms(), logs_dir=str(tmp_path))
        snap = build_combined(rts, now=TS)
        assert set(snap["arms"]) == {"a", "b"}
        assert snap["arms"]["a"]["note"] == "two names"
        assert snap["arms"]["a"]["universe"] == ["HDFCBANK", "ICICIBANK"]
        assert "totals" in snap["arms"]["a"] and "risk" in snap["arms"]["a"]


class TestArmRegistry:
    def test_union_universe_within_50(self):
        u = all_universe_symbols()
        assert len(u) <= 50 and len(u) == len(set(u))   # deduped, within feed limit

    def test_control_arm_exists(self):
        assert any(a.name == "control" for a in ARMS)
