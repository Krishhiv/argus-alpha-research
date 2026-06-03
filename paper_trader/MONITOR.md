# Paper Trader — Live Monitor

A minimal, dependency-free monitoring dashboard for the paper trader, modelled on
the collector's telemetry terminal. Same security model: it binds to `127.0.0.1`
only and is reached over an SSH tunnel — **nothing is exposed publicly**.

## What it shows

- **Total / Realized / Unrealized P&L** — realized from the trade log, unrealized
  marked from the live position state.
- **Win rate, payoff, avg win/loss, trades, fill rate.**
- **Day-risk gauge** — day P&L vs the −₹20,000 circuit breaker.
- **Intraday cumulative P&L** — the headline equity curve (with a live dashed
  segment for open-position mark-to-market).
- **Open positions** — side, entry, mid, qty, unrealized P&L (live).
- **Per-instrument** realized breakdown and **exit-method** breakdown
  (maker_exit / taker_max_hold / taker_stop).

Realized metrics come from the durable CSV logs, so the dashboard works even when
the trader is stopped (e.g. reviewing after close). Live panels (positions,
unrealized, feed, breaker) populate only while the trader is running; otherwise the
status pill shows `OFFLINE`.

## Architecture

```
running trader ──writes──> logs/paper_telemetry.json   (live state, every 2s, atomic)
                └─appends─> logs/paper_trades.csv       (durable realized history)
                                   │
serve_monitor.py ──reads both──> /api/monitor (merged JSON)
                └──serves──────> dashboard/ (static terminal UI, polls at 1 Hz)
```

No Flask/React/Chart.js — Python stdlib `http.server` + vanilla HTML/CSS/JS with a
hand-drawn canvas chart.

## Run it

**1. Start the monitor server on the VPS** (in an SSH session):

```bash
ssh lightsail-mumbai
cd /home/ubuntu/paper-trader
venv/bin/python -m paper_trader.monitor.serve_monitor --port 8082
```

**2. Open an SSH tunnel from your laptop** (separate terminal):

```bash
ssh -N -L 8082:127.0.0.1:8082 lightsail-mumbai
```

**3. Open the dashboard** in your browser:

```
http://127.0.0.1:8082
```

Keep the tunnel terminal open while viewing. `Ctrl-C` the server when done.

## Notes

- The monitor is **read-only** — it never touches trading state, only reads the
  snapshot and CSV. A crash or restart of the monitor has zero effect on the trader.
- The trader writes the live snapshot itself (an asyncio task in `main.py`), so the
  monitor has nothing to start/stop on the trading side.
- Default port is 8082 (the collector dashboard uses 8081 — pick distinct ports if
  tunnelling both at once).
