#!/usr/bin/env bash
# Overnight PUBG Shorts calibration — ingest + Telegram feed until target.
set -Eeuo pipefail
REPO="${VPS_REPO_PATH:-/root/content_bot_ml}"
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
LOG=/root/data/pubg/logs/pubg_shorts_night_batch.log
mkdir -p /root/data/pubg/logs /root/datasets/pubg/youtube_shorts

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

export PUBG_SHORTS_CALIBRATION=1
export PUBG_SHORTS_NIGHT_TARGET="${PUBG_SHORTS_NIGHT_TARGET:-100}"
export PUBG_CALIBRATION_BATCH="${PUBG_CALIBRATION_BATCH:-8}"
export PUBG_INGEST_MAX_DOWNLOADS="${PUBG_INGEST_MAX_DOWNLOADS:-12}"
export PUBG_SHORTS_NIGHT_SLEEP_SEC="${PUBG_SHORTS_NIGHT_SLEEP_SEC:-20}"
export PUBG_SHORTS_METRO_TAG="${PUBG_SHORTS_METRO_TAG:-0}"
export CONTENT_BOT_REPO="$REPO"

echo "[$(date -Is)] pubg_shorts_night_run target=$PUBG_SHORTS_NIGHT_TARGET" >>"$LOG"
exec python3 -u "$REPO/scripts/pubg_shorts_night_batch.py" >>"$LOG" 2>&1
