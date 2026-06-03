"""
Serve the paper-trader monitor dashboard from localhost.

Stdlib ThreadingHTTPServer (no framework). Serves the static terminal UI plus
one /api/monitor endpoint that merges:
  - realized metrics + equity curve from the durable trade CSV (always available)
  - live state (open positions, unrealized PnL, breaker, feed) from the running
    trader's telemetry snapshot (when the trader is up)

Bind 127.0.0.1 and reach it over an SSH tunnel — nothing is exposed publicly.

Run on the VPS:
    venv/bin/python -m paper_trader.monitor.serve_monitor --port 8082
Tunnel from your laptop:
    ssh -N -L 8082:127.0.0.1:8082 lightsail-mumbai
Then open http://127.0.0.1:8082
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

if __package__ in {None, ""}:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paper_trader.config import TRADES_LOG, TELEMETRY_PATH
from paper_trader.monitor.metrics import read_trades, realized_metrics, today_ist
from paper_trader.telemetry import load_snapshot

logger = logging.getLogger("argus.monitor")

_STATIC_DIR = Path(__file__).resolve().parent / "dashboard"
_LIVE_STALE_SEC = 15.0   # snapshot older than this → trader considered offline


def build_payload(trades_path: Path, telemetry_path: Path) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    date = today_ist()
    realized = realized_metrics(read_trades(trades_path), date)

    live: dict[str, Any] | None = None
    live_online = False
    try:
        live = load_snapshot(telemetry_path)
        gen = live.get("generated_at")
        if gen:
            age = (now - datetime.fromisoformat(gen)).total_seconds()
            live_online = age < _LIVE_STALE_SEC
            live["age_sec"] = round(age, 1)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        live = None

    return {
        "server_time": now.isoformat(),
        "date": date,
        "live_online": live_online,
        "realized": realized,
        "live": live,
    }


class _MonitorHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(_STATIC_DIR), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/monitor":
            self._serve_api()
            return
        if path in {"/", ""}:
            self.path = "/index.html"
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("monitor_http %s", fmt % args)

    def _serve_api(self) -> None:
        try:
            payload = build_payload(self.server.trades_path, self.server.telemetry_path)  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, payload)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Serve the paper-trader monitor dashboard.")
    p.add_argument("--host", default="127.0.0.1", help="Bind host (keep 127.0.0.1 for SSH tunneling).")
    p.add_argument("--port", type=int, default=8082, help="Bind port.")
    p.add_argument("--trades-path", default=TRADES_LOG, help="Trade CSV path.")
    p.add_argument("--telemetry-path", default=TELEMETRY_PATH, help="Live telemetry JSON path.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    server = ThreadingHTTPServer((args.host, args.port), _MonitorHandler)
    server.trades_path = Path(args.trades_path).expanduser().resolve()        # type: ignore[attr-defined]
    server.telemetry_path = Path(args.telemetry_path).expanduser().resolve()  # type: ignore[attr-defined]

    logger.info("Monitor on http://%s:%d", args.host, args.port)
    logger.info("Trades:    %s", server.trades_path)      # type: ignore[attr-defined]
    logger.info("Telemetry: %s", server.telemetry_path)   # type: ignore[attr-defined]
    logger.info("Tunnel: ssh -N -L %d:127.0.0.1:%d lightsail-mumbai", args.port, args.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("monitor interrupted; shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
