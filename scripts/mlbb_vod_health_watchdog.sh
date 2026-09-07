#!/usr/bin/env bash
# Health tick for VOD-only: kill Shorts zombies, ensure systemd feed is up.
# NEVER nohup the feed supervisor — systemd is the sole owner.
set -uo pipefail
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
LOG=/root/data/mlbb/logs/mlbb_vod_health.log
BIN=/usr/local/bin
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
UNIT="${VOD_FEED_SYSTEMD_UNIT:-content-bot-vod-feed.service}"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date -Is)] $*" >>"$LOG"; }

restart_vod_feed() {
  if systemctl cat "$UNIT" >/dev/null 2>&1; then
    systemctl restart "$UNIT"
    return 0
  fi
  if systemctl cat content-bot-vod-feed.service >/dev/null 2>&1; then
    systemctl restart content-bot-vod-feed.service
    return 0
  fi
  log "REFUSED nohup feed restart — install unit via deploy_unified_production.sh"
  return 1
}

restart_telegram_bot() {
  for u in telegram-upload-bot.service content-bot-telegram.service; do
    if systemctl cat "$u" >/dev/null 2>&1; then
      systemctl restart "$u"
      return 0
    fi
  done
  if [[ "${VOD_FEED_ALLOW_NOHUP:-0}" == "1" ]]; then
    nohup python3 "$BIN/telegram_upload_bot.py" >>/root/data/mlbb/telegram_upload_bot.log 2>&1 &
    return 0
  fi
  log "REFUSED nohup telegram — enable unit or set VOD_FEED_ALLOW_NOHUP=1"
  return 1
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
  # Still enforce systemd feed if unit exists (pubg-only unified world).
  :
fi

for pat in highlight_train.py mlbb_continuous_worker mlbb_calibration_feed \
  mlbb_youtube_shorts_ingest mlbb_hero_shorts_montage; do
  if pgrep -f "$pat" >/dev/null 2>&1; then
    log "kill forbidden $pat"
    pkill -9 -f "$pat" 2>/dev/null || true
  fi
done

if ! pgrep -f 'telegram_upload_bot.py' >/dev/null 2>&1; then
  log "restart telegram_upload_bot via systemd"
  restart_telegram_bot || true
elif [[ "$(pgrep -fc 'telegram_upload_bot.py' || echo 0)" -gt 1 ]]; then
  log "duplicate telegram_upload_bot — restart unit"
  pkill -9 -f 'telegram_upload_bot.py' 2>/dev/null || true
  sleep 1
  restart_telegram_bot || true
fi

FEED_UP=0
if systemctl is-active "$UNIT" >/dev/null 2>&1 \
  || systemctl is-active content-bot-vod-feed.service >/dev/null 2>&1; then
  FEED_UP=1
fi
if [[ "$FEED_UP" -eq 0 ]] \
  && ! pgrep -f 'mlbb_vod_segment_feed.sh' >/dev/null 2>&1 \
  && ! pgrep -f 'daily_cycle_runner.py' >/dev/null 2>&1 \
  && ! pgrep -f 'shooter_vod_segment_feed.py' >/dev/null 2>&1 \
  && ! pgrep -f 'mlbb_vod_segment_feed.py' >/dev/null 2>&1; then
  log "feed down — systemd restart only"
  rm -f /tmp/pubg_vod_segment_feed.lock /tmp/mlbb_vod_segment_feed.lock /tmp/mlbb_vod_supervisor.lock
  restart_vod_feed || true
fi

DISK_PCT="$(df / | awk 'NR==2 {gsub(/%/,""); print $5}')"
if [[ -n "$DISK_PCT" && "$DISK_PCT" -ge "${VOD_CLEANUP_MAX_USED_PCT:-88}" ]]; then
  log "disk critical ${DISK_PCT}% — run vps_disk_cleanup"
  if [[ -x "$REPO/scripts/vps_disk_cleanup.sh" ]]; then
    bash "$REPO/scripts/vps_disk_cleanup.sh" >>"$LOG" 2>&1 || true
  fi
fi

DETECTOR="$REPO/scripts/vod_hang_detector.py"
if [[ ! -f "$DETECTOR" ]]; then
  DETECTOR="$BIN/vod_hang_detector.py"
fi
if [[ -f "$DETECTOR" ]]; then
  while IFS='=' read -r key val; do
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    export "$key"="$val"
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" 2>/dev/null | grep -v 'FORMAT\|YTDL\|yt_dlp\|YT_DLP' || true)
  export CONTENT_BOT_REPO="$REPO"
  python3 "$DETECTOR" --tick >>"$LOG" 2>&1 || log "hang detector tick failed"
fi

exit 0
