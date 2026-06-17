#!/usr/bin/env bash
# Pause VOD worker → send 10 HQ Shorts → restore VOD mode.
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
BIN=/usr/local/bin
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
ROLLBACK="${ROLLBACK:-/root/data/mlbb/rollback_pre_hq_shorts.env}"
LOG="${LOG:-/root/data/mlbb/hq_shorts_mission.log}"
TARGET="${TARGET:-10}"

mkdir -p /root/data/mlbb /root/datasets/mlbb/youtube_shorts_hq_mission

echo "===== HQ shorts mission $(date -Is) =====" | tee -a "$LOG"

# 1) Snapshot for rollback
cp -a "$ENV_FILE" "$ROLLBACK"
echo "rollback saved → $ROLLBACK" | tee -a "$LOG"

# 2) Pause VOD pipeline
pkill -f mlbb_continuous_worker.py 2>/dev/null || true
pkill -f mlbb_vod_segment_feed.py 2>/dev/null || true
pkill -f mlbb_vod_montage_feed.py 2>/dev/null || true
pkill -f mlbb_calibration_feed.py 2>/dev/null || true
sleep 2
echo "VOD worker paused" | tee -a "$LOG"

# 3) Mission
export YOUTUBE_SHORTS_FORMAT_HQ="${YOUTUBE_SHORTS_FORMAT_HQ:-bv*[vcodec^=avc1][height<=1080][height>=720]+ba/bv*[height<=1080][height>=720]+ba/bv*[height<=1080]+ba/b[height<=1080]/best}"
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

if python3 "$BIN/mlbb_hq_shorts_mission.py" --target "$TARGET" >>"$LOG" 2>&1; then
  echo "mission OK — restoring VOD" | tee -a "$LOG"
  RESTORE=1
else
  echo "mission incomplete — env kept, VOD still paused (run mlbb_mission_rollback.sh restore)" | tee -a "$LOG"
  RESTORE=0
fi

# 4) Restore VOD mode on success
if [[ "$RESTORE" == "1" ]]; then
  cp -a "$ROLLBACK" "$ENV_FILE"
  bash "$REPO/scripts/install_mlbb_only_mode.sh" >>"$LOG" 2>&1 || true
  echo "VOD mode restored $(date -Is)" | tee -a "$LOG"
fi

exit "$([[ "$RESTORE" == "1" ]] && echo 0 || echo 1)"
