#!/usr/bin/env bash
# Step 1: finish owner 8-URL batch (no parallel feed).
# Step 2: start base VOD feed via systemd only (never nohup dual-owner).
# Step 3: feed keeps running until a new manual command stops it.
set -Eeuo pipefail

ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
LOG="${MLBB_OWNER_THEN_FEED_LOG:-/root/data/mlbb/owner_then_feed.log}"
BATCH="$REPO/scripts/run_owner_batch8.sh"
OWNER_LOCK="/root/data/mlbb/OWNER_BATCH_RUNNING"
UNIT="${VOD_FEED_SYSTEMD_UNIT:-content-bot-vod-feed.service}"

mkdir -p "$(dirname "$LOG")"

log() {
  echo "[$(date -Is)] $*" >>"$LOG"
}

set_vod_disabled() {
  local val="$1"
  if grep -q '^MLBB_VOD_DISABLED=' "$ENV_FILE" 2>/dev/null; then
    sed -i "s/^MLBB_VOD_DISABLED=.*/MLBB_VOD_DISABLED=${val}/" "$ENV_FILE"
  else
    echo "MLBB_VOD_DISABLED=${val}" >>"$ENV_FILE"
  fi
}

stop_feed_only() {
  systemctl stop "$UNIT" 2>/dev/null || true
  systemctl stop content-bot-vod-feed.service 2>/dev/null || true
  pkill -TERM -f 'mlbb_vod_segment_feed.py' 2>/dev/null || true
  pkill -TERM -f 'mlbb_vod_segment_feed.sh' 2>/dev/null || true
  pkill -TERM -f 'shooter_vod_segment_feed.py' 2>/dev/null || true
  sleep 2
  pkill -KILL -f 'mlbb_vod_segment_feed.py' 2>/dev/null || true
  pkill -KILL -f 'mlbb_vod_segment_feed.sh' 2>/dev/null || true
  pkill -KILL -f 'shooter_vod_segment_feed.py' 2>/dev/null || true
  rm -f /tmp/mlbb_vod_segment_feed.lock /tmp/mlbb_vod_supervisor.lock /tmp/pubg_vod_segment_feed.lock
}

stop_vod_pipeline() {
  stop_feed_only
  if [[ ! -f "$OWNER_LOCK" ]]; then
    pkill -TERM -f 'mlbb_vod_oneoff.py' 2>/dev/null || true
    sleep 2
    pkill -KILL -f 'mlbb_vod_oneoff.py' 2>/dev/null || true
    rm -f /tmp/mlbb_vod_oneoff.lock
  fi
}

wait_feed_alive() {
  sleep 3
  if systemctl is-active "$UNIT" >/dev/null 2>&1 \
    || systemctl is-active content-bot-vod-feed.service >/dev/null 2>&1 \
    || pgrep -f 'mlbb_vod_segment_feed.sh' >/dev/null 2>&1 \
    || pgrep -f 'shooter_vod_segment_feed.py' >/dev/null 2>&1 \
    || pgrep -f 'mlbb_vod_segment_feed.py' >/dev/null 2>&1; then
    log "feed ok (systemd/process visible)"
    return 0
  fi
  log "ERROR: feed failed to start via systemd"
  return 1
}

start_feed_systemd() {
  if systemctl cat "$UNIT" >/dev/null 2>&1; then
    systemctl reset-failed "$UNIT" 2>/dev/null || true
    systemctl enable --now "$UNIT"
    return 0
  fi
  if systemctl cat content-bot-vod-feed.service >/dev/null 2>&1; then
    systemctl reset-failed content-bot-vod-feed.service 2>/dev/null || true
    systemctl enable --now content-bot-vod-feed.service
    return 0
  fi
  log "REFUSED nohup feed — run: bash $REPO/scripts/deploy_unified_production.sh"
  return 1
}

[[ -f "$ENV_FILE" ]] || { log "missing $ENV_FILE"; exit 1; }
[[ -x "$BATCH" ]] || { log "missing $BATCH"; exit 1; }

log "=== STEP 1: owner batch (8 URLs), feed paused ==="
set_vod_disabled 1
echo "owner_batch $(date -Is)" >"$OWNER_LOCK"
stop_feed_only

set +u
set -a
# shellcheck disable=SC1091
source "$ENV_FILE" 2>/dev/null || true
set +a
set -u

export PYTHONPATH="$REPO/scripts"
export MLBB_VOD_ONLY=1 MLBB_ONLY_MODE=1
export MLBB_VOD_LENIENT_UNIFORM=0 MLBB_VOD_TAIL_MIN_HUD_RATE=0.58
export MLBB_KILL_BANNER_COLOR_ONLY=0
export MLBB_VOD_SEND_ONE=0 MLBB_KILL_BANNER_MIN_TIER=double
export HIGHLIGHT_MAX_PANN_PROBE=5 HIGHLIGHT_MAX_STAGE1=16
export MLBB_VOD_PROBE_LIMIT=16 MLBB_VOD_SKIP_REVALIDATE=1

log "running $BATCH"
set +e
bash "$BATCH" >>"$LOG" 2>&1
batch_rc=$?
set -e
rm -f "$OWNER_LOCK"

if [[ "$batch_rc" -eq 143 || "$batch_rc" -eq 137 ]]; then
  log "owner batch interrupted rc=$batch_rc — NOT starting base feed"
  exit "$batch_rc"
fi
if [[ "$batch_rc" -eq 0 ]]; then
  log "owner batch finished ok"
else
  log "owner batch finished rc=$batch_rc (some URLs may have sent=0; continuing to base feed)"
fi

log "=== STEP 2: base VOD feed via systemd (no nohup) ==="
set_vod_disabled 0
stop_feed_only
start_feed_systemd || exit 1
log "started $UNIT"

wait_feed_alive || exit 1
log "=== STEP 3: base feed running until new command ==="
