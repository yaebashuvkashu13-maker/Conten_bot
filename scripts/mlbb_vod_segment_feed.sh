#!/usr/bin/env bash
# One VOD feed supervisor — systemd is the only owner.
# flock prevents a second copy; exit 0 on contention so RestartSec backs off.
set -Eeuo pipefail
exec 9>/tmp/mlbb_vod_supervisor.lock
if ! flock -n 9; then
  echo "$(date -Is) another mlbb_vod supervisor running — exit" >&2
  exit 0
fi
# Do NOT source /root/.video_bot.env here — yt-dlp format strings contain []
# which bash glob-expands and kills the supervisor. Python loads env safely.
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
export CONTENT_BOT_REPO="$REPO"
export PYTHONPATH="/usr/local/bin:${REPO}/scripts${PYTHONPATH:+:$PYTHONPATH}"
export VOD_PUBG_ONLY="${VOD_PUBG_ONLY:-1}"
export HIGHLIGHT_HEATMAP="${HIGHLIGHT_HEATMAP:-0}"
IDLE_SEC="${MLBB_VOD_IDLE_SEC:-25}"
LOG_DIR="${VOD_FEED_LOG_DIR:-/root/data/mlbb}"
mkdir -p "$LOG_DIR"
RUNNER="$REPO/scripts/daily_cycle_runner.py"
if [[ ! -f "$RUNNER" ]]; then
  RUNNER=/usr/local/bin/daily_cycle_runner.py
fi
while true; do
  python3 -u "$RUNNER" >>"$LOG_DIR/mlbb_vod_segment_feed.log" 2>&1 || true
  sleep "$IDLE_SEC"
done
