#!/usr/bin/env bash
# Faster TikTok harvest (still SOCKS5, short pauses between batches).
set -Eeuo pipefail
LOG=/root/data/mlbb/fast_loop.log
exec >>"$LOG" 2>&1
export PYTHONPATH=/usr/local/bin:${PYTHONPATH:-}
if [[ -f /root/.video_bot.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /root/.video_bot.env
  set +a
fi
export MASS_HUMAN_MODE=1
export MASS_CSV_ONLY=0
export MASS_DOWNLOAD_WORKERS="${MASS_DOWNLOAD_WORKERS:-7}"
export MASS_SESSION_LIMIT="${MASS_SESSION_LIMIT:-60}"
export MASS_DELAY_MIN="${MASS_DELAY_MIN:-2.5}"
export MASS_DELAY_MAX="${MASS_DELAY_MAX:-7.5}"
export MASS_DOWNLOAD_TARGET="${MASS_DOWNLOAD_TARGET:-5000}"
PAUSE_MIN="${FAST_PAUSE_MIN:-120}"
PAUSE_MAX="${FAST_PAUSE_MAX:-240}"

echo "[$(date)] FAST loop workers=$MASS_DOWNLOAD_WORKERS session=$MASS_SESSION_LIMIT target=$MASS_DOWNLOAD_TARGET"
while true; do
  if ! pgrep -f tiktok_mass_download.py >/dev/null; then
    echo "[$(date)] batch mp4=$(find /root/datasets/tiktok/mlbb -name '*.mp4' 2>/dev/null | wc -l)"
    python3 /usr/local/bin/tiktok_mass_download.py --human \
      --target "$MASS_DOWNLOAD_TARGET" --workers "$MASS_DOWNLOAD_WORKERS" || true
    tail -2 /root/data/mlbb/mass_download.log || true
  fi
  sleep $((PAUSE_MIN + RANDOM % (PAUSE_MAX - PAUSE_MIN + 1)))
done
