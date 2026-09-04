#!/usr/bin/env bash
# Keep VPS in VOD-only shape: kill Shorts/zombies, ensure one VOD feed alive.
# Hang detection + auto-recover: scripts/vod_hang_detector.py --tick
set -uo pipefail
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
LOG=/root/data/mlbb/logs/mlbb_vod_health.log
BIN=/usr/local/bin
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date -Is)] $*" >>"$LOG"; }

restart_vod_feed() {
  if systemctl cat content-bot-vod-feed.service >/dev/null 2>&1; then
    systemctl restart content-bot-vod-feed.service
  else
    nohup "$BIN/mlbb_vod_segment_feed.sh" >>/root/data/mlbb/vod_only_supervisor.log 2>&1 &
  fi
}

restart_telegram_bot() {
  if systemctl cat telegram-upload-bot.service >/dev/null 2>&1; then
    systemctl restart telegram-upload-bot.service
  else
    nohup python3 "$BIN/telegram_upload_bot.py" >>/root/data/mlbb/telegram_upload_bot.log 2>&1 &
  fi
}

env_val() {
  local key="$1"
  grep "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'"
}

[[ -f "$ENV_FILE" ]] || exit 0
if [[ -f /root/data/mlbb/OWNER_BATCH_RUNNING ]]; then
  log "owner batch lock — skip health watchdog"
  exit 0
fi
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
  restart_telegram_bot
elif [[ "$(pgrep -fc 'telegram_upload_bot.py' || echo 0)" -gt 1 ]]; then
  log "duplicate telegram_upload_bot — keep one instance"
  pkill -9 -f 'telegram_upload_bot.py' 2>/dev/null || true
  sleep 1
  restart_telegram_bot
fi

if ! pgrep -f 'mlbb_vod_segment_feed.sh' >/dev/null 2>&1 \
  && ! pgrep -f 'daily_cycle_runner.py' >/dev/null 2>&1 \
  && ! pgrep -f 'shooter_vod_segment_feed.py' >/dev/null 2>&1 \
  && ! pgrep -f 'mlbb_vod_segment_feed.py' >/dev/null 2>&1; then
  log "restart vod supervisor"
  rm -f /tmp/pubg_vod_segment_feed.lock /tmp/mlbb_vod_segment_feed.lock
  restart_vod_feed
fi

# Disk emergency — inbox cleanup when root is nearly full (common silence cause).
DISK_PCT="$(df / | awk 'NR==2 {gsub(/%/,""); print $5}')"
if [[ -n "$DISK_PCT" && "$DISK_PCT" -ge "${VOD_CLEANUP_MAX_USED_PCT:-88}" ]]; then
  log "disk critical ${DISK_PCT}% — run vps_disk_cleanup"
  if [[ -x "$REPO/scripts/vps_disk_cleanup.sh" ]]; then
    bash "$REPO/scripts/vps_disk_cleanup.sh" >>"$LOG" 2>&1 || true
  fi
fi

# Real hang detector: silence, zero-send drought, stuck ffmpeg/yt-dlp, bad inbox loop.
DETECTOR="$REPO/scripts/vod_hang_detector.py"
if [[ ! -f "$DETECTOR" ]]; then
  DETECTOR="$BIN/vod_hang_detector.py"
fi
if [[ -f "$DETECTOR" ]]; then
  # Load only safe key=value lines — skip yt-dlp format strings with [] that bash glob-expands
  while IFS='=' read -r key val; do
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    export "$key"="$val"
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" 2>/dev/null | grep -v 'FORMAT\|YTDL\|yt_dlp\|YT_DLP' || true)
  export CONTENT_BOT_REPO="$REPO"
  # Keepalive agent defaults: heal after 1h silence, soften+skip discovery, retry every 10m.
  export VOD_SILENCE_WARN_SEC="${VOD_SILENCE_WARN_SEC:-3600}"
  export VOD_SILENCE_HEAL_SEC="${VOD_SILENCE_HEAL_SEC:-3600}"
  export VOD_SILENCE_ALERT_SEC="${VOD_SILENCE_ALERT_SEC:-10800}"
  export VOD_ABSOLUTE_SILENCE_SEC="${VOD_ABSOLUTE_SILENCE_SEC:-5400}"
  export VOD_FORCE_DROUGHT_SEC="${VOD_FORCE_DROUGHT_SEC:-3600}"
  export VOD_PROGRESS_STUCK_SEC="${VOD_PROGRESS_STUCK_SEC:-900}"
  export VOD_ZERO_SEND_STREAK_HEAL="${VOD_ZERO_SEND_STREAK_HEAL:-6}"
  export VOD_HEAL_COOLDOWN_SEC="${VOD_HEAL_COOLDOWN_SEC:-2700}"
  export VOD_HEAL_RETRY_SEC="${VOD_HEAL_RETRY_SEC:-600}"
  export VOD_HEAL_RETRY_ON_SILENCE="${VOD_HEAL_RETRY_ON_SILENCE:-1}"
  export VOD_HEAL_BACKGROUND="${VOD_HEAL_BACKGROUND:-1}"
  export VOD_RECOVER_UNPARK="${VOD_RECOVER_UNPARK:-4}"
  export VOD_FORCE_SEND_MAX_VODS="${VOD_FORCE_SEND_MAX_VODS:-4}"
  export VOD_RECOVER_FORCE_SEND_TIMEOUT_SEC="${VOD_RECOVER_FORCE_SEND_TIMEOUT_SEC:-1800}"
  export VOD_CHILD_STUCK_SEC="${VOD_CHILD_STUCK_SEC:-600}"
  export YOUTUBE_DOWNLOAD_TIMEOUT="${YOUTUBE_DOWNLOAD_TIMEOUT:-2400}"
  python3 "$DETECTOR" --tick >>"$LOG" 2>&1 || log "hang detector tick failed"
else
  log "WARN: vod_hang_detector.py missing — legacy stuck check only"
  FEED_LOG=/root/data/mlbb/mlbb_vod_segment_feed.log
  if [[ -f "$FEED_LOG" ]]; then
    LOG_AGE_SEC=$(( $(date +%s) - $(stat -c %Y "$FEED_LOG" 2>/dev/null || echo 0) ))
    STUCK_SEC="${MLBB_VOD_FEED_STUCK_SEC:-1800}"
    if [[ "$LOG_AGE_SEC" -gt "$STUCK_SEC" ]] && pgrep -f 'shooter_vod_segment_feed.py' >/dev/null 2>&1; then
      log "feed stuck log_age=${LOG_AGE_SEC}s — restart"
      systemctl restart content-bot-vod-feed.service 2>/dev/null || restart_vod_feed
    fi
  fi
fi

exit 0
