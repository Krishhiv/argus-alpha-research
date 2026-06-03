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


# ── payload merge ───────────────────────────────────────────────────────────

class TestBuildPayload:
    def _write_csv(self, path: Path, date: str):
        header = "underlying,date,net_pnl,gross_pnl,fee,exit_method,exit_ts\n"
        rows = (f"ICICIBANK,{date},100,120,20,maker_exit,{date}T04:00:01\n"
                f"RELIANCE,{date},-40,-20,20,taker_stop,{date}T04:00:02\n")
        path.write_text(header + rows)

    def test_realized_from_csv_when_no_live(self, tmp_path: Path):
        csv = tmp_path / "trades.csv"
        self._write_csv(csv, today_ist())
        payload = build_payload(csv, tmp_path / "absent.json")
        assert payload["live_online"] is False
        assert payload["live"] is None
        assert payload["realized"]["n_trades"] == 2
        assert payload["realized"]["net_pnl"] == 60.0

    def test_live_online_when_snapshot_fresh(self, tmp_path: Path):
        csv = tmp_path / "trades.csv"
        self._write_csv(csv, today_ist())
        tele = tmp_path / "telemetry.json"
        snap = {"generated_at": datetime.now(UTC).isoformat(),
                "totals": {"unrealized_pnl": 50.0, "open_positions": 1, "n_posts": 5, "n_fills": 2},
                "risk": {"halted": False, "day_net_pnl": 60.0, "loss_limit": -20000.0},
                "feed": {"last_packet_age_sec": 0.4}, "brokers": {}}
        tele.write_text(json.dumps(snap))
        payload = build_payload(csv, tele)
        assert payload["live_online"] is True
        assert payload["live"]["totals"]["unrealized_pnl"] == 50.0

    def test_live_offline_when_snapshot_stale(self, tmp_path: Path):
        csv = tmp_path / "trades.csv"
        self._write_csv(csv, today_ist())
        tele = tmp_path / "telemetry.json"
        old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        tele.write_text(json.dumps({"generated_at": old, "totals": {}, "risk": {}, "feed": {}, "brokers": {}}))
        payload = build_payload(csv, tele)
        assert payload["live_online"] is False
