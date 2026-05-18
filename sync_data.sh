#!/usr/bin/env bash
# sync_data.sh — pull Argus tick data from the VPS to data/raw/
#
# Usage:
#   ./sync_data.sh            — live sync
#   ./sync_data.sh --dry-run  — show what would be transferred, touch nothing
#
# The VPS directory structure is:
#   /home/ubuntu/data/tbt-dhan/{feed}/trading_date={YYYY-MM-DD}/symbol={INSTRUMENT}/
#       compacted-{feed}-{date}-{instrument}-{id}.parquet
#
# After sync, data/raw/ mirrors this structure exactly.
# data/ is gitignored — never commit parquet files.
#
# Excluded:
#   - market/ feed (high latency, not used — depth feed only strategy)
#   - symbol=BAJFINANCE (data quality too poor)
#   NOTE: verify the market feed directory name on the VPS matches MARKET_FEED below.

set -euo pipefail

REMOTE_HOST="lightsail-mumbai"
REMOTE_PATH="/home/ubuntu/data/tbt-dhan/"
LOCAL_PATH="$(cd "$(dirname "$0")" && pwd)/data/raw/"

# Verify this matches the actual feed directory name on the VPS
MARKET_FEED="market"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

# Ensure local destination exists
mkdir -p "$LOCAL_PATH"

RSYNC_OPTS=(
  --archive           # preserve permissions, timestamps, symlinks
  --compress          # compress during transfer (parquet compresses well)
  --human-readable
  --progress
  --stats
  --exclude="${MARKET_FEED}/"        # skip entire market feed (depth-only strategy)
  --exclude='symbol=BAJFINANCE*/'    # skip BAJFINANCE (poor data quality; wildcard covers contract-month suffix)
  --include='*/'                     # traverse all other Hive partition directories
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

# Print a summary of what's now in data/raw/
if [[ $DRY_RUN -eq 0 ]]; then
  echo ""
  echo "Files in data/raw/ by feed and date:"
  find "$LOCAL_PATH" -name "*.parquet" \
    | sed "s|${LOCAL_PATH}||" \
    | awk -F'/' '{print $1 "/" $2}' \
    | sort -u \
    | while read -r partition; do
        count=$(find "${LOCAL_PATH}${partition}" -name "*.parquet" 2>/dev/null | wc -l | tr -d ' ')
        size=$(du -sh "${LOCAL_PATH}${partition}" 2>/dev/null | cut -f1)
        printf "  %-60s %5s files  %s\n" "$partition" "$count" "$size"
      done

  echo ""
  total_files=$(find "$LOCAL_PATH" -name "*.parquet" | wc -l | tr -d ' ')
  total_size=$(du -sh "$LOCAL_PATH" 2>/dev/null | cut -f1)
  echo "  Total: ${total_files} parquet files, ${total_size}"
fi
