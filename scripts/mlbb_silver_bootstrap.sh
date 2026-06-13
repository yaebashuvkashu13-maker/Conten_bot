#!/usr/bin/env bash
# One-shot silver dataset bootstrap + train. Safe to re-run.
set -Eeuo pipefail
set -a
source /root/.video_bot.env
set +a
export CONTENT_BOT_REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
export MLBB_DATA_ROOT="${MLBB_DATA_ROOT:-/root/data/mlbb}"
export PYTHONPATH="/usr/local/bin:${CONTENT_BOT_REPO}/scripts:${PYTHONPATH:-}"
export HIGHLIGHT_HEATMAP=0
export HIGHLIGHT_USE_OWNER_ANCHORS=0
export MLBB_USE_CLASSIFIER=1
export MLBB_LEARNING_FIRST=0
export MLBB_SEND_ENABLED=1

LOCK="${LOCK:-/tmp/mlbb_silver_bootstrap.lock}"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "mlbb_silver_bootstrap already running"
  exit 0
fi

python3 /usr/local/bin/mlbb_silver_bootstrap.py \
  --hero-limit "${MLBB_SILVER_HERO_LIMIT:-24}" \
  --telegram-limit "${MLBB_SILVER_TELEGRAM_LIMIT:-80}" \
  --tiktok-limit "${MLBB_SILVER_TIKTOK_LIMIT:-40}" \
  --youtube-downloads "${MLBB_SILVER_YT_DOWNLOADS:-30}" \
  --viral-downloads "${MLBB_SILVER_VIRAL_DOWNLOADS:-25}" \
  --telegram \
  "$@"

echo "mlbb_silver_bootstrap done $(date -Is)"
