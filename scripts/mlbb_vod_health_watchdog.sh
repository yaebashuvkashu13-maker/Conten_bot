#!/usr/bin/env bash
# Keep VPS in VOD-only shape: kill Shorts/zombies, ensure one VOD feed alive.
set -Eeuo pipefail
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
LOG=/root/data/mlbb/logs/mlbb_vod_health.log
BIN=/usr/local/bin
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date -Is)] $*" >>"$LOG"; }

env_val() {
  local key="$1"
  grep "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'"
}

[[ -f "$ENV_FILE" ]] || exit 0
if [[ "$(env_val MLBB_VOD_ONLY)" != "1" || "$(env_val MLBB_VOD_DISABLED)" == "1" ]]; then
  exit 0
fi

# Zombies that have repeatedly killed the box.
for pat in highlight_train.py mlbb_continuous_worker mlbb_calibration_feed \
  mlbb_youtube_shorts_ingest mlbb_hero_shorts_montage; do
  if pgrep -f "$pat" >/dev/null 2>&1; then
    log "kill forbidden $pat"
    pkill -9 -f "$pat" 2>/dev/null || true
  fi
done

# Stuck owner sync one-liners from manual debugging.
pgrep -af 'sync_owner_learning' 2>/dev/null | grep -v mlbb_vod_segment_feed | while read -r line; do
  pid=$(echo "$line" | awk '{print $1}')
  [[ -n "$pid" ]] && kill -9 "$pid" 2>/dev/null && log "kill stuck sync pid=$pid"
done

if ! pgrep -f 'telegram_upload_bot.py' >/dev/null 2>&1; then
  log "restart telegram_upload_bot"
  nohup python3 "$BIN/telegram_upload_bot.py" >>/root/data/mlbb/telegram_upload_bot.log 2>&1 &
fi

if ! pgrep -f 'mlbb_vod_segment_feed.py' >/dev/null 2>&1; then
  log "restart vod supervisor"
  pkill -f 'mlbb_vod_segment_feed.sh' 2>/dev/null || true
  sleep 1
  rm -f /tmp/mlbb_vod_segment_feed.lock
  nohup "$BIN/mlbb_vod_segment_feed.sh" >>/root/data/mlbb/vod_only_supervisor.log 2>&1 &
fi

exit 0
