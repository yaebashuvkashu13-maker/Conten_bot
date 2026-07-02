#!/usr/bin/env bash
# ~18:00 MSK start → montages for 5 games, ready by ~08:00 MSK (YouTube VOD, no proxy).
set -Eeuo pipefail

LOCK=/var/lock/overnight_msk.lock
LOG=/root/data/mlbb/overnight_msk/cron.log
mkdir -p /root/data/mlbb/overnight_msk

exec >>"$LOG" 2>&1
if [[ -f /root/.video_bot.env ]]; then set -a; source /root/.video_bot.env; set +a; fi
if [[ "${MLBB_ONLY_MODE:-0}" == "1" ]]; then
  echo "[$(date -Is)] SKIP overnight_msk: MLBB_ONLY_MODE=1"
  exit 0
fi
echo "[$(date -Is)] overnight_msk start pid=$$"

if [[ -f /root/.video_bot.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /root/.video_bot.env
  set +a
fi

unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy YTDLP_PROXY

if [[ -x /usr/local/bin/stop_competing_workers.sh ]]; then
  /usr/local/bin/stop_competing_workers.sh || true
elif [[ -x /root/content_bot_ml/scripts/stop_competing_workers.sh ]]; then
  bash /root/content_bot_ml/scripts/stop_competing_workers.sh || true
fi

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -Is)] skip: already running"
  exit 0
fi

export OVERNIGHT_GAMES_CONFIG="${OVERNIGHT_GAMES_CONFIG:-/root/content_bot_ml/config/overnight_games.yaml}"
export OVERNIGHT_DEADLINE_HOUR_MSK="${OVERNIGHT_DEADLINE_HOUR_MSK:-8}"
export SMART_LONG_ANALYSIS_MAX_FPS="${SMART_LONG_ANALYSIS_MAX_FPS:-0.25}"
export PYTHONPATH="/root/content_bot_ml/scripts:/root/content_bot_ml:${PYTHONPATH:-}"

python3 /usr/local/bin/overnight_youtube_batch.py
rc=$?
echo "[$(date -Is)] overnight_msk done rc=$rc"
exit "$rc"
