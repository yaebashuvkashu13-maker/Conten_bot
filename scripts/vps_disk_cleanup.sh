#!/usr/bin/env bash
# Safe VPS disk cleanup — watermark policy, protects active and owner-labeled VODs.
set -Eeuo pipefail

echo "BEFORE: $(df -h / | tail -1)"
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
CLEANER="$REPO/scripts/vod_disk_cleanup.py"
[[ -f "$CLEANER" ]] || CLEANER=/usr/local/bin/vod_disk_cleanup.py
[[ -f "$CLEANER" ]] || { echo "cleanup script missing"; exit 1; }

ACTIVE_GAME="${VOD_CLEANUP_ACTIVE_GAME:-pubg}"
if [[ ! -f /root/data/mlbb/EU_PUBG_ONLY ]] \
  && ! grep -q '^VOD_PUBG_ONLY=1' /root/.video_bot.env 2>/dev/null; then
  ACTIVE_GAME="${VOD_CLEANUP_ACTIVE_GAME:-mlbb}"
fi

python3 "$CLEANER" \
  --active-game "$ACTIVE_GAME" \
  --min-free-gb "${VOD_CLEANUP_MIN_FREE_GB:-15}" \
  --target-free-gb "${VOD_CLEANUP_TARGET_FREE_GB:-25}" \
  --max-used-pct "${VOD_CLEANUP_MAX_USED_PCT:-88}" \
  "$@"
echo "AFTER: $(df -h / | tail -1)"
