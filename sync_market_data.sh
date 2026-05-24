#!/usr/bin/env bash
# sync_market_data.sh — pull market (trade print) feed from the VPS to data/raw/market/
#
# Usage:
#   ./sync_market_data.sh            — live sync
#   ./sync_market_data.sh --dry-run  — show what would be transferred, touch nothing
#
# VPS structure (verified 2026-05-24):
#   /home/ubuntu/data/tbt-dhan/market/trading_date={YYYY-MM-DD}/symbol={INSTRUMENT}-{Month}-FUT/
#       compacted-market-{date}-{instrument}-{id}.parquet
#
# Market feed contains tick-by-tick trade prints (price, qty, timestamp).
# Used for fill validation: confirm whether a trade actually occurred at a
# posted limit price, rather than relying on bid/ask level-change proxies.
# Note: market feed packets have ~2s latency vs depth feed — suitable for
# backtesting fill validation only, not for live signal generation.
#
# After sync, data/raw/market/ mirrors the VPS partition structure exactly.
# data/ is gitignored — never commit parquet files.
#
# Excluded:
#   - symbol=BAJFINANCE (data quality too poor, consistent with depth sync)

set -euo pipefail

REMOTE_HOST="lightsail-mumbai"
REMOTE_PATH="/home/ubuntu/data/tbt-dhan/market/"
LOCAL_PATH="$(cd "$(dirname "$0")" && pwd)/data/raw/market/"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

mkdir -p "$LOCAL_PATH"

RSYNC_OPTS=(
  --archive           # preserve permissions, timestamps, symlinks
  --compress          # compress during transfer
  --human-readable
  --progress
  --stats
  --exclude='symbol=BAJFINANCE*/'    # skip BAJFINANCE (poor data quality)
  --include='*/'                     # traverse all Hive partition directories
  --include='compacted-*.parquet'    # only fully compacted daily files
  --exclude='*'                      # skip fragmented/in-progress files
)

if [[ $DRY_RUN -eq 1 ]]; then
  RSYNC_OPTS+=(--dry-run)
  echo "=== DRY RUN — no files will be transferred ==="
fi

echo "Source : ${REMOTE_HOST}:${REMOTE_PATH}"
echo "Dest   : ${LOCAL_PATH}"
echo "---"

rsync "${RSYNC_OPTS[@]}" \
  "${REMOTE_HOST}:${REMOTE_PATH}" \
  "${LOCAL_PATH}"

echo ""
echo "=== Sync complete ==="

if [[ $DRY_RUN -eq 0 ]]; then
  echo ""
  echo "Files in data/raw/market/ by date:"
  find "$LOCAL_PATH" -name "*.parquet" \
    | sed "s|${LOCAL_PATH}||" \
    | awk -F'/' '{print $1}' \
    | sort -u \
    | while read -r partition; do
        count=$(find "${LOCAL_PATH}${partition}" -name "*.parquet" 2>/dev/null | wc -l | tr -d ' ')
        size=$(du -sh "${LOCAL_PATH}${partition}" 2>/dev/null | cut -f1)
        printf "  %-45s %5s files  %s\n" "$partition" "$count" "$size"
      done

  echo ""
  total_files=$(find "$LOCAL_PATH" -name "*.parquet" | wc -l | tr -d ' ')
  total_size=$(du -sh "$LOCAL_PATH" 2>/dev/null | cut -f1)
  echo "  Total: ${total_files} parquet files, ${total_size}"
fi
