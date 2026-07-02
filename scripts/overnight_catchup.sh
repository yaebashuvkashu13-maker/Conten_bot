#!/usr/bin/env bash
# Resume overnight batch: retries, skip finished games, work ~10h.
set -Eeuo pipefail

LOG=/root/data/mlbb/overnight_msk/catchup.log
mkdir -p /root/data/mlbb/overnight_msk

exec >>"$LOG" 2>&1
if [[ -f /root/.video_bot.env ]]; then set -a; source /root/.video_bot.env; set +a; fi
if [[ "${MLBB_ONLY_MODE:-0}" == "1" ]]; then
  echo "[$(date -Is)] SKIP overnight_catchup: MLBB_ONLY_MODE=1"
  exit 0
fi
echo "[$(date -Is)] overnight_catchup start"

if [[ -x /usr/local/bin/stop_competing_workers.sh ]]; then
  /usr/local/bin/stop_competing_workers.sh || true
fi

if [[ -f /root/.video_bot.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /root/.video_bot.env
  set +a
fi

unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy YTDLP_PROXY

export OVERNIGHT_GAMES_CONFIG="${OVERNIGHT_GAMES_CONFIG:-/root/content_bot_ml/config/overnight_games.yaml}"
export PYTHONPATH="/root/content_bot_ml/scripts:/root/content_bot_ml:${PYTHONPATH:-}"
export OVERNIGHT_STOP_IN_HOURS="${OVERNIGHT_STOP_IN_HOURS:-10}"
export OVERNIGHT_DOWNLOAD_RETRIES="${OVERNIGHT_DOWNLOAD_RETRIES:-3}"
export OVERNIGHT_MONTAGE_RETRIES="${OVERNIGHT_MONTAGE_RETRIES:-2}"

exec 9>/var/lock/overnight_msk.lock
if ! flock -n 9; then
  echo "[$(date -Is)] skip: batch already running"
  exit 0
fi

python3 /usr/local/bin/overnight_youtube_batch.py --resume --stop-in-hours "$OVERNIGHT_STOP_IN_HOURS"
rc=$?
echo "[$(date -Is)] overnight_catchup done rc=$rc"
exit "$rc"
