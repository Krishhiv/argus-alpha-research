#!/usr/bin/env bash
#
# One-shot Argus paper-trading status — prints the multi-arm leaderboard and
# per-arm breakdown, then exits. Uses a single short SSH command (no sustained
# tunnel), so it stays reliable even when the lossy route to the VPS drops the
# live dashboard's tunnel.
#
#   ./status.sh
#
# Override the host with ARGUS_VPS_HOST (default: lightsail-mumbai).
#
set -euo pipefail

VPS_HOST="${ARGUS_VPS_HOST:-lightsail-mumbai}"
REMOTE_DIR="${ARGUS_REMOTE_DIR:-/home/ubuntu/paper-trader}"

ssh -o ConnectTimeout=10 "$VPS_HOST" \
    "cd ${REMOTE_DIR} && venv/bin/python -m paper_trader.monitor.status"
