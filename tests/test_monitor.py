"""Tests for the paper-trader monitor: realized metrics, live snapshot, payload merge."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from paper_trader.broker import PaperBroker, DayRisk
from paper_trader.telemetry import build_snapshot, write_snapshot_atomic, load_snapshot
from paper_trader.monitor.metrics import realized_metrics, read_trades, today_ist
from paper_trader.monitor.serve_monitor import build_payload

UTC = timezone.utc
SESSION_TS = datetime(2026, 6, 4, 4, 0, 0, tzinfo=UTC)


# ── realized metrics ──────────────────────────────────────────────────────────

def _row(date, sym, net, gross, fee, method, ts):
    return {"date": date, "underlying": sym, "net_pnl": str(net), "gross_pnl": str(gross),
            "fee": str(fee), "exit_method": method, "exit_ts": ts}


class TestRealizedMetrics:
    def test_empty(self):
        m = realized_metrics([], "2026-06-04")
        assert m["n_trades"] == 0 and m["net_pnl"] == 0.0 and m["equity_curve"] == []

    def test_basic_stats(self):
        rows = [
            _row("2026-06-04", "ICICIBANK", 100, 120, 20, "maker_exit", "2026-06-04T04:00:01"),
            _row("2026-06-04", "ICICIBANK", -50, -30, 20, "taker_max_hold", "2026-06-04T04:00:02"),
            _row("2026-06-03", "RELIANCE", 999, 999, 0, "maker_exit", "2026-06-03T04:00:00"),  # other day
        ]
        m = realized_metrics(rows, "2026-06-04")
        assert m["n_trades"] == 2
        assert m["net_pnl"] == 50.0
        assert m["win_rate"] == 0.5
        assert m["avg_win"] == 100.0
        assert m["avg_loss"] == -50.0
        assert m["payoff"] == 2.0
        assert m["best"] == 100.0 and m["worst"] == -50.0

    def test_per_instrument_and_exits(self):
        rows = [
            _row("2026-06-04", "ICICIBANK", 100, 120, 20, "maker_exit", "2026-06-04T04:00:01"),
            _row("2026-06-04", "RELIANCE", -50, -30, 20, "taker_stop", "2026-06-04T04:00:02"),
        ]
        m = realized_metrics(rows, "2026-06-04")
        assert m["per_instrument"]["ICICIBANK"]["net"] == 100.0
        assert m["per_instrument"]["ICICIBANK"]["win_rate"] == 1.0
        assert m["exit_breakdown"]["taker_stop"]["n"] == 1
        assert m["exit_breakdown"]["taker_stop"]["net"] == -50.0

    def test_equity_curve_is_cumulative_and_ordered(self):
        rows = [
            _row("2026-06-04", "X", 30, 30, 0, "maker_exit", "2026-06-04T04:00:03"),
            _row("2026-06-04", "X", 10, 10, 0, "maker_exit", "2026-06-04T04:00:01"),
            _row("2026-06-04", "X", -5, -5, 0, "maker_exit", "2026-06-04T04:00:02"),
        ]
        ec = realized_metrics(rows, "2026-06-04")["equity_curve"]
        assert [p["cum"] for p in ec] == [10.0, 5.0, 35.0]   # ordered by exit_ts, cumulative

    def test_read_trades_missing_file(self, tmp_path: Path):
        assert read_trades(tmp_path / "nope.csv") == []


# ── live snapshot ───────────────────────────────────────────────────────────

class TestSnapshot:
    def _open_long(self) -> tuple[dict, DayRisk]:
        risk = DayRisk(-20000.0)
        brokers = {s: PaperBroker(s, risk=risk) for s in ["HDFCBANK", "ICICIBANK"]}
        b = brokers["ICICIBANK"]
        b.on_depth_packet(ts_utc=SESSION_TS, bid_price=1450.0, bid_qty=990, ask_price=1451.0, ask_qty=10)
        b.on_depth_packet(ts_utc=SESSION_TS, bid_price=1449.5, bid_qty=500, ask_price=1451.0, ask_qty=10)
        b.on_depth_packet(ts_utc=SESSION_TS, bid_price=1450.5, bid_qty=500, ask_price=1451.5, ask_qty=500)
        return brokers, risk

    def test_snapshot_totals_and_unrealized(self):
        brokers, risk = self._open_long()
        snap = build_snapshot(brokers, risk, now=SESSION_TS)
        assert snap["totals"]["open_positions"] == 1
        assert snap["totals"]["unrealized_pnl"] == pytest.approx(700.0)  # (1451-1450)*700
        assert snap["brokers"]["ICICIBANK"]["position_side"] == 1
        assert snap["brokers"]["HDFCBANK"]["position_side"] == 0
        assert snap["risk"]["halted"] is False

    def test_atomic_write_load_roundtrip(self, tmp_path: Path):
        brokers, risk = self._open_long()
        snap = build_snapshot(brokers, risk, now=SESSION_TS)
        path = tmp_path / "telemetry.json"
        write_snapshot_atomic(path, snap)
        loaded = load_snapshot(path)
        assert loaded["totals"]["unrealized_pnl"] == pytest.approx(700.0)


# ── multi-arm metrics + payload merge ────────────────────────────────────────

import paper_trader.monitor.metrics as M
from paper_trader.monitor.metrics import discover_arms, realized_for_arms, cumulative_for_arm

_HDR = "underlying,date,net_pnl,gross_pnl,fee,exit_method,exit_ts\n"


def _make_arms(base: Path, date: str):
    """Two arms with today's trades under base/<arm>/paper_trades.csv."""
    (base / "control").mkdir(parents=True)
    (base / "control" / "paper_trades.csv").write_text(
        _HDR + f"ICICIBANK,{date},100,120,20,maker_exit,{date}T04:00:01\n"
             + f"RELIANCE,{date},-40,-20,20,taker_stop,{date}T04:00:02\n")
    (base / "no_stop").mkdir(parents=True)
    (base / "no_stop" / "paper_trades.csv").write_text(
        _HDR + f"RELIANCE,{date},80,100,20,maker_exit,{date}T04:00:01\n")


class TestMultiArmMetrics:
    def test_discover_and_realized(self, tmp_path: Path):
        d = today_ist()
        _make_arms(tmp_path, d)
        assert discover_arms(str(tmp_path)) == ["control", "no_stop"]
        r = realized_for_arms(d, str(tmp_path))
        assert r["control"]["net_pnl"] == 60.0 and r["control"]["n_trades"] == 2
        assert r["no_stop"]["net_pnl"] == 80.0

    def test_cumulative(self, tmp_path: Path):
        d = today_ist()
        _make_arms(tmp_path, d)
        c = cumulative_for_arm("control", str(tmp_path))
        assert c["total_net"] == 60.0 and c["n_trades"] == 2 and c["n_days"] == 1


class TestBuildPayload:
    def test_multi_arm_merge_online(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(M, "ARMS_BASE", str(tmp_path / "arms"))
        d = today_ist()
        _make_arms(tmp_path / "arms", d)
        tele = tmp_path / "telemetry.json"
        tele.write_text(json.dumps({
            "generated_at": datetime.now(UTC).isoformat(),
            "arms": {"control": {
                "totals": {"unrealized_pnl": 50.0, "open_positions": 1, "n_posts": 20, "n_fills": 2},
                "risk": {"halted": False, "day_net_pnl": 100.0, "loss_limit": -20000.0},
                "feed": {"last_packet_age_sec": 0.3}, "brokers": {},
                "note": "baseline", "universe": ["ICICIBANK", "RELIANCE"]}}}))
        payload = build_payload(tele)
        assert payload["live_online"] is True
        assert set(payload["arms"]) == {"control", "no_stop"}
        assert payload["arms"]["control"]["realized"]["net_pnl"] == 60.0
        assert payload["arms"]["control"]["live"]["totals"]["unrealized_pnl"] == 50.0
        assert payload["arms"]["control"]["note"] == "baseline"
        assert payload["arms"]["no_stop"]["live"] is None   # no live snapshot for that arm

    def test_offline_still_shows_realized(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(M, "ARMS_BASE", str(tmp_path / "arms"))
        d = today_ist()
        _make_arms(tmp_path / "arms", d)
        tele = tmp_path / "telemetry.json"
        old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        tele.write_text(json.dumps({"generated_at": old, "arms": {}}))
        payload = build_payload(tele)
        assert payload["live_online"] is False
        assert payload["arms"]["control"]["realized"]["net_pnl"] == 60.0

    def test_missing_telemetry_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(M, "ARMS_BASE", str(tmp_path / "arms"))
        d = today_ist()
        _make_arms(tmp_path / "arms", d)
        payload = build_payload(tmp_path / "absent.json")
        assert payload["live_online"] is False
        assert "control" in payload["arms"]
