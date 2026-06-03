#!/usr/bin/env bash
#
# Open the Argus paper-trader live monitor from your laptop in one command.
#
#   ./open_monitor.sh
#
# It ensures the monitor server is running on the VPS, opens an SSH tunnel,
# waits until the dashboard responds, and launches your browser. Press Ctrl-C
# to tear everything down (the tunnel always closes; the server is stopped only
# if this script started it).
#
# Overridable via env vars:
#   ARGUS_VPS_HOST     (default: lightsail-mumbai)
#   ARGUS_MONITOR_PORT (default: 8082)
#   ARGUS_REMOTE_DIR   (default: /home/ubuntu/paper-trader)
#
set -euo pipefail

VPS_HOST="${ARGUS_VPS_HOST:-lightsail-mumbai}"
PORT="${ARGUS_MONITOR_PORT:-8082}"
REMOTE_DIR="${ARGUS_REMOTE_DIR:-/home/ubuntu/paper-trader}"
URL="http://127.0.0.1:${PORT}"
PGREP_PAT="serve_monitor.*--port ${PORT}"

started_by_us=0
tunnel_pid=""

cleanup() {
  echo
  if [[ -n "$tunnel_pid" ]] && kill "$tunnel_pid" 2>/dev/null; then
    echo "• tunnel closed"
  fi
  if [[ "$started_by_us" == "1" ]]; then
    ssh "$VPS_HOST" "pkill -f '${PGREP_PAT}'" 2>/dev/null && echo "• monitor server stopped on VPS" || true
  fi
}
trap cleanup EXIT INT TERM

echo "Argus paper monitor  →  ${VPS_HOST}  (port ${PORT})"

# 1. Ensure the monitor server is running on the VPS (idempotent).
if ssh "$VPS_HOST" "pgrep -f '${PGREP_PAT}' >/dev/null 2>&1"; then
  echo "• monitor server already running on VPS"
else
  echo "• starting monitor server on VPS..."
  ssh "$VPS_HOST" "cd ${REMOTE_DIR} && nohup venv/bin/python -m paper_trader.monitor.serve_monitor --port ${PORT} </dev/null >/tmp/argus_monitor.log 2>&1 &"
  started_by_us=1
  sleep 1
fi

# 2. Open the SSH tunnel (background).
echo "• tunnel  localhost:${PORT}  →  ${VPS_HOST}:127.0.0.1:${PORT}"
ssh -N -L "${PORT}:127.0.0.1:${PORT}" "$VPS_HOST" &
tunnel_pid=$!

# 3. Wait for the dashboard to answer through the tunnel.
printf "• waiting for monitor"
ready=0
for _ in $(seq 1 20); do
  if curl -fsS -o /dev/null "${URL}/api/monitor" 2>/dev/null; then
    ready=1; printf " — ready\n"; break
  fi
  printf "."; sleep 0.5
done
if [[ "$ready" != "1" ]]; then
  printf " — timeout\n"
  echo "✗ monitor did not respond at ${URL} (check /tmp/argus_monitor.log on the VPS)"
  exit 1
fi

# 4. Open the browser.
if command -v open >/dev/null 2>&1; then
  open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL"
else
  echo "• open this in your browser: ${URL}"
fi

echo
echo "  Monitor live →  ${URL}"
echo "  Ctrl-C to close the tunnel and exit."
wait "$tunnel_pid"
