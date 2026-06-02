#!/usr/bin/env bash
# Overnight gentle TikTok harvest (human-like). Run in tmux/nohup.
set -Eeuo pipefail
LOG=/root/data/mlbb/night_loop.log
exec >>"$LOG" 2>&1
export PYTHONPATH=/usr/local/bin:${PYTHONPATH:-}
if [[ -f /root/.video_bot.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /root/.video_bot.env
  set +a
fi
export MASS_HUMAN_MODE=1
export MASS_CSV_ONLY=1
export MASS_DOWNLOAD_WORKERS="${MASS_DOWNLOAD_WORKERS:-3}"
export MASS_SESSION_LIMIT="${MASS_SESSION_LIMIT:-30}"
export MASS_DELAY_MIN="${MASS_DELAY_MIN:-9}"
export MASS_DELAY_MAX="${MASS_DELAY_MAX:-26}"
export MASS_DOWNLOAD_TARGET="${MASS_DOWNLOAD_TARGET:-250}"
PAUSE_MIN="${NIGHT_PAUSE_MIN:-900}"
PAUSE_MAX="${NIGHT_PAUSE_MAX:-1500}"

echo "[$(date)] night loop start workers=$MASS_DOWNLOAD_WORKERS session=$MASS_SESSION_LIMIT"
while true; do
  if ! pgrep -f tiktok_mass_download.py >/dev/null; then
    echo "[$(date)] batch start mp4=$(find /root/datasets/tiktok/mlbb -name '*.mp4' 2>/dev/null | wc -l)"
    python3 /usr/local/bin/tiktok_mass_download.py --human --csv-only \
      --target "$MASS_DOWNLOAD_TARGET" --workers "$MASS_DOWNLOAD_WORKERS" \
      || true
    echo "[$(date)] batch done stats tail:"
    tail -3 /root/data/mlbb/mass_download.log || true
  fi
  sleep $((PAUSE_MIN + RANDOM % (PAUSE_MAX - PAUSE_MIN + 1)))
done
