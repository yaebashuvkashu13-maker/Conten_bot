#!/usr/bin/env bash
# Auto-send OCR/owner-positive kill-banner screenshots to owner (cron).
set -Eeuo pipefail
set -a
source /root/.video_bot.env
set +a
export CONTENT_BOT_REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
export PYTHONPATH="/usr/local/bin:${CONTENT_BOT_REPO}/scripts"
export PYTHONUNBUFFERED=1
export MLBB_BANNER_REF_VOD_CROP="${MLBB_BANNER_REF_VOD_CROP:-0}"
export MLBB_QUICK_SEND_BATCH="${MLBB_QUICK_SEND_BATCH:-15}"
export MLBB_QUICK_SEND_SEGMENT_FIRST="${MLBB_QUICK_SEND_SEGMENT_FIRST:-1}"
export MLBB_BANNER_POS_POV_MATCH="${MLBB_BANNER_POS_POV_MATCH:-0}"
export MLBB_POS_SCAN_SKIP_INNER_LOCK=1
export MLBB_POS_SCAN_BATCH="${MLBB_POS_SCAN_BATCH:-20}"
export MLBB_POS_SCAN_VODS="${MLBB_POS_SCAN_VODS:-30}"
export MLBB_POS_SCAN_SAMPLES="${MLBB_POS_SCAN_SAMPLES:-12}"
export MLBB_POS_SCAN_MIN_TIER="${MLBB_POS_SCAN_MIN_TIER:-2}"
export MLBB_POS_SCAN_MIN_SCORE="${MLBB_POS_SCAN_MIN_SCORE:-4}"
export MLBB_POS_SCAN_T0="${MLBB_POS_SCAN_T0:-90}"
export MLBB_POS_SCAN_T1="${MLBB_POS_SCAN_T1:-1200}"
LOG=/root/data/mlbb/logs/mlbb_banner_positive_scan.log
mkdir -p /root/data/mlbb/logs
LOCK=/tmp/mlbb_banner_positive_scan.lock
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) skip: another positive scan running" >>"$LOG"
  exit 0
fi
echo "===== $(date -Is) positive scan =====" >>"$LOG"
python3 "${CONTENT_BOT_REPO}/scripts/mlbb_banner_calibration_quick_send.py" >>"$LOG" 2>&1 \
  || python3 "${CONTENT_BOT_REPO}/scripts/mlbb_banner_calibration_scan_send.py" >>"$LOG" 2>&1
