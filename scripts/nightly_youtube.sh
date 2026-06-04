#!/usr/bin/env bash
# Nightly MLBB YouTube: discover 1.5–3.5h streams → download → Smart Edit → Telegram.
# Cron (MSK example): 30 1 * * * /usr/local/bin/nightly_youtube.sh
set -Eeuo pipefail
LOCK=/var/lock/youtube_nightly.lock
LOG=/root/data/mlbb/youtube_nightly/cron.log
mkdir -p /root/data/mlbb/youtube_nightly
exec >>"$LOG" 2>&1
echo "[$(date)] nightly_youtube start"

if [[ -f /root/.video_bot.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /root/.video_bot.env
  set +a
fi
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy YTDLP_PROXY

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date)] already running, exit"
  exit 0
fi

python3 /usr/local/bin/nightly_youtube_montage.py
echo "[$(date)] nightly_youtube done rc=$?"
