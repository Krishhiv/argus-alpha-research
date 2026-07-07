# Paper Trader - Live Monitor

A minimal, dependency-free monitoring dashboard for the paper trader, modelled on
the collector's telemetry terminal. Same security model: it binds to `127.0.0.1`
only and is reached over an SSH tunnel - **nothing is exposed publicly**.

## What it shows

The trader runs several strategy **arms** in parallel (see `paper_trader/arms.py`)
on one shared feed. The dashboard has two layers:

- **Arm leaderboard** (top) - every arm ranked by total P&L, with realized,
  unrealized, trades, win rate and day-risk. Click an arm to drill in.
- **Selected-arm detail** (below) - for the chosen arm:
  - **Total / Realized / Unrealized P&L**, win rate, payoff, trades, fill rate.
  - **Day-risk gauge** - day P&L vs the −₹20,000 circuit breaker.
  - **Intraday cumulative P&L** - the headline equity curve (with a live dashed
    segment for open-position mark-to-market).
  - **Open positions**, **per-instrument** breakdown, and **exit-method**
    breakdown (maker_exit / taker_max_hold / taker_stop / taker_reversal).

Realized metrics come from each arm's durable CSV logs, so the dashboard works even
when the trader is stopped (reviewing after close). Live panels (positions,
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

No Flask/React/Chart.js - Python stdlib `http.server` + vanilla HTML/CSS/JS with a
hand-drawn canvas chart.

## Run it - one command (recommended)

From the repo root on your laptop:

```bash
./open_monitor.sh
```

This ensures the server is running on the VPS, opens the SSH tunnel, waits for the
dashboard to respond, and launches your browser. Press `Ctrl-C` to close the tunnel
and exit (the server is stopped only if the script started it). Override defaults
with `ARGUS_VPS_HOST`, `ARGUS_MONITOR_PORT`, `ARGUS_REMOTE_DIR` env vars.

## Run it - manual (fallback)

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

**3. Open the dashboard** in your browser: `http://127.0.0.1:8082`

Keep the tunnel terminal open while viewing. `Ctrl-C` the server when done.

## Update rate

The running trader writes the live snapshot at **1 Hz** (`TELEMETRY_INTERVAL_SEC`),
and the dashboard polls `/api/monitor` at **1 Hz** - so open positions and
unrealized P&L refresh roughly every second, matching the collector's telemetry
terminal. Realized metrics and the equity curve update the moment each trade closes
(the CSV is read fresh on every poll). Live panels populate only while the trader is
running (market hours); otherwise the status pill reads `OFFLINE` and realized
history still shows.

## Notes

- The monitor is **read-only** - it never touches trading state, only reads the
  snapshot and CSV. A crash or restart of the monitor has zero effect on the trader.
- The trader writes the live snapshot itself (an asyncio task in `main.py`), so the
  monitor has nothing to start/stop on the trading side.
- Default port is 8082 (the collector dashboard uses 8081 - pick distinct ports if
  tunnelling both at once).
