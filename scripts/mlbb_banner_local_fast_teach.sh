#!/usr/bin/env bash
# OCR-first owner teach: scan already-downloaded VODs, send only verified kill banners.
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
export MLBB_BANNER_USE_SMART_TEACH=1
export MLBB_BANNER_FLOOD_REQUIRE_OCR=1
# Keep owner negatives active — do NOT disable gates in teach.
export MLBB_BANNER_SEND_STRICT="${MLBB_BANNER_SEND_STRICT:-1}"
export MLBB_BANNER_NEG_REF_MATCH="${MLBB_BANNER_NEG_REF_MATCH:-1}"
export MLBB_BANNER_POS_REQUIRE_EDGE=1
export MLBB_BANNER_NEG_REQUIRE_EDGE=1
export MLBB_BANNER_POS_POV_MATCH=0
export MLBB_BANNER_OCR_NO_BANNER_MIN_SIM=0.90
export MLBB_BANNER_CALIB_TARGET="${MLBB_BANNER_CALIB_TARGET:-500}"
export MLBB_BANNER_FLOOD_MAX="${MLBB_BANNER_FLOOD_MAX:-20}"
export MLBB_SMART_TEACH_MAX="${MLBB_SMART_TEACH_MAX:-${MLBB_BANNER_FLOOD_MAX}}"
export MLBB_SMART_TEACH_VODS="${MLBB_SMART_TEACH_VODS:-30}"
export MLBB_SMART_TEACH_PEAKS="${MLBB_SMART_TEACH_PEAKS:-16}"
export MLBB_SMART_TEACH_MIN_TIER="${MLBB_SMART_TEACH_MIN_TIER:-2}"
export MLBB_BANNER_FLOOD_DELAY_SEC="${MLBB_BANNER_FLOOD_DELAY_SEC:-0.25}"
export MLBB_VOD_INBOX="${MLBB_VOD_INBOX:-/root/data/mlbb/youtube_nightly/inbox}"

LOG=/root/data/mlbb/logs/mlbb_banner_local_fast_teach.log
mkdir -p /root/data/mlbb/logs
LOCK=/tmp/mlbb_banner_local_fast_teach.lock
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) skip: local fast teach already running" >>"$LOG"
  exit 0
fi

echo "===== $(date -Is) smart OCR teach inbox=$(ls "$MLBB_VOD_INBOX"/yt_*.mp4 2>/dev/null | wc -l) max=${MLBB_BANNER_FLOOD_MAX} =====" >>"$LOG"
python3 "${CONTENT_BOT_REPO}/scripts/mlbb_banner_smart_teach.py" >>"$LOG" 2>&1
echo "===== $(date -Is) done =====" >>"$LOG"
