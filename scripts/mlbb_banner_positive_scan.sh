#!/usr/bin/env bash
# Auto-send OCR + owner-learned kill-banner screenshots (cron).
set -Eeuo pipefail
set -a
source /root/.video_bot.env
set +a
export CONTENT_BOT_REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
export PYTHONPATH="/usr/local/bin:${CONTENT_BOT_REPO}/scripts"
export PYTHONUNBUFFERED=1
export MLBB_BANNER_REF_VOD_CROP="${MLBB_BANNER_REF_VOD_CROP:-0}"
export MLBB_BANNER_SEND_STRICT="${MLBB_BANNER_SEND_STRICT:-1}"
export MLBB_QUICK_SEND_SEGMENT_FIRST=0
export MLBB_BANNER_POS_POV_MATCH="${MLBB_BANNER_POS_POV_MATCH:-0}"
export MLBB_POS_SCAN_SKIP_INNER_LOCK=1
export MLBB_QUICK_SEND_BATCH="${MLBB_QUICK_SEND_BATCH:-8}"
export MLBB_QUICK_SEND_VODS="${MLBB_QUICK_SEND_VODS:-8}"
export MLBB_QUICK_SEND_STEP_SEC="${MLBB_QUICK_SEND_STEP_SEC:-60}"
export MLBB_QUICK_SEND_T0="${MLBB_QUICK_SEND_T0:-30}"
export MLBB_QUICK_SEND_T1="${MLBB_QUICK_SEND_T1:-1500}"
export MLBB_QUICK_SEND_SEGMENT_PEAKS="${MLBB_QUICK_SEND_SEGMENT_PEAKS:-1}"
export MLBB_POS_SCAN_BATCH="${MLBB_POS_SCAN_BATCH:-12}"
export MLBB_POS_SCAN_VODS="${MLBB_POS_SCAN_VODS:-10}"
export MLBB_POS_SCAN_SAMPLES="${MLBB_POS_SCAN_SAMPLES:-10}"
export MLBB_POS_SCAN_MIN_TIER="${MLBB_POS_SCAN_MIN_TIER:-2}"
export MLBB_POS_SCAN_MIN_SCORE="${MLBB_POS_SCAN_MIN_SCORE:-4}"
LOG=/root/data/mlbb/logs/mlbb_banner_positive_scan.log
mkdir -p /root/data/mlbb/logs
LOCK=/tmp/mlbb_banner_positive_scan.lock
if [[ -f "$LOCK" ]] && find "$LOCK" -mmin +25 2>/dev/null | grep -q .; then
  if ! pgrep -f mlbb_banner_calibration_quick_send.py >/dev/null 2>&1; then
    rm -f "$LOCK"
  fi
fi
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) skip: another positive scan running" >>"$LOG"
  exit 0
fi
echo "===== $(date -Is) positive scan =====" >>"$LOG"
python3 "${CONTENT_BOT_REPO}/scripts/mlbb_banner_calibration_quick_send.py" >>"$LOG" 2>&1
