#!/usr/bin/env bash
# Fastest owner-teach path: scan ONLY already-downloaded VODs (no YouTube download).
set -Eeuo pipefail
set -a
source /root/.video_bot.env
set +a
export CONTENT_BOT_REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
export MLBB_BANNER_REF_ROOT="${MLBB_BANNER_REF_ROOT:-${CONTENT_BOT_REPO}/data/mlbb_kill_banners}"
export PYTHONPATH="/usr/local/bin:${CONTENT_BOT_REPO}/scripts"
export PYTHONUNBUFFERED=1

# No downloads — inbox only.
export MLBB_VOD_AUTO_DOWNLOAD=0
export MLBB_BANNER_TEACH_FLOOD=1
export MLBB_BANNER_SEND_STRICT=0
export MLBB_BANNER_NEG_REF_MATCH=0
export MLBB_BANNER_POS_POV_MATCH=0
export MLBB_BANNER_OCR_NO_BANNER_MIN_SIM=0.95
export MLBB_BANNER_CALIB_TARGET=500
export MLBB_BANNER_FLOOD_EXTRA=80
export MLBB_BANNER_FLOOD_MAX="${MLBB_BANNER_FLOOD_MAX:-40}"
export MLBB_BANNER_FLOOD_SEGMENT_LIMIT="${MLBB_BANNER_FLOOD_SEGMENT_LIMIT:-40}"
export MLBB_BANNER_FLOOD_SCAN_LIMIT="${MLBB_BANNER_FLOOD_SCAN_LIMIT:-40}"
export MLBB_BANNER_FLOOD_VODS="${MLBB_BANNER_FLOOD_VODS:-60}"
export MLBB_BANNER_FLOOD_SAMPLES="${MLBB_BANNER_FLOOD_SAMPLES:-14}"
export MLBB_BANNER_FLOOD_T0="${MLBB_BANNER_FLOOD_T0:-40}"
export MLBB_BANNER_FLOOD_T1="${MLBB_BANNER_FLOOD_T1:-2000}"
export MLBB_BANNER_FLOOD_DELAY_SEC="${MLBB_BANNER_FLOOD_DELAY_SEC:-0.2}"
export MLBB_VOD_INBOX="${MLBB_VOD_INBOX:-/root/data/mlbb/youtube_nightly/inbox}"

LOG=/root/data/mlbb/logs/mlbb_banner_local_fast_teach.log
mkdir -p /root/data/mlbb/logs
LOCK=/tmp/mlbb_banner_local_fast_teach.lock
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) skip: local fast teach already running" >>"$LOG"
  exit 0
fi

echo "===== $(date -Is) local fast teach inbox=$(ls "$MLBB_VOD_INBOX"/yt_*.mp4 2>/dev/null | wc -l) max=${MLBB_BANNER_FLOOD_MAX} =====" >>"$LOG"
python3 "${CONTENT_BOT_REPO}/scripts/mlbb_banner_calibration_flood.py" >>"$LOG" 2>&1
echo "===== $(date -Is) done =====" >>"$LOG"
