#!/usr/bin/env bash
# Roll back to state before HQ shorts mission.
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
ROLLBACK="${ROLLBACK:-/root/data/mlbb/rollback_pre_hq_shorts.env}"

if [[ ! -f "$ROLLBACK" ]]; then
  echo "No rollback file at $ROLLBACK"
  exit 1
fi

pkill -f mlbb_hq_shorts_mission 2>/dev/null || true
pkill -f mlbb_continuous_worker.py 2>/dev/null || true
pkill -f mlbb_vod_segment_feed.py 2>/dev/null || true
sleep 1

cp -a "$ROLLBACK" "$ENV_FILE"
bash "$REPO/scripts/install_mlbb_only_mode.sh"
echo "OK rolled back from $ROLLBACK and restarted VOD worker"
