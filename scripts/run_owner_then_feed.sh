#!/usr/bin/env bash
# Step 1: finish owner 8-URL batch (no parallel feed).
# Step 2: start base VOD feed (download → highlights → cut → send).
# Step 3: feed keeps running until a new manual command stops it.
set -Eeuo pipefail

ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
LOG="${MLBB_OWNER_THEN_FEED_LOG:-/root/data/mlbb/owner_then_feed.log}"
BATCH="$REPO/scripts/run_owner_batch8.sh"
OWNER_LOCK="/root/data/mlbb/OWNER_BATCH_RUNNING"

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
  pkill -TERM -f 'mlbb_vod_segment_feed.py' 2>/dev/null || true
  pkill -TERM -f 'mlbb_vod_segment_feed.sh' 2>/dev/null || true
  sleep 2
  pkill -KILL -f 'mlbb_vod_segment_feed.py' 2>/dev/null || true
  pkill -KILL -f 'mlbb_vod_segment_feed.sh' 2>/dev/null || true
  rm -f /tmp/mlbb_vod_segment_feed.lock
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
  if pgrep -f 'mlbb_vod_segment_feed.py' >/dev/null 2>&1; then
    log "feed ok pid=$(pgrep -f 'mlbb_vod_segment_feed.py' | head -1)"
    return 0
  fi
  log "ERROR: feed failed to start"
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
export MLBB_VOD_LENIENT_UNIFORM=1 MLBB_VOD_TAIL_MIN_HUD_RATE=0.45
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

log "=== STEP 2: base VOD feed (no owner oneoff) ==="
set_vod_disabled 0
stop_feed_only
rm -f /tmp/mlbb_vod_segment_feed.lock

FEED_WRAPPER="/usr/local/bin/mlbb_vod_segment_feed.sh"
if [[ ! -x "$FEED_WRAPPER" ]]; then
  FEED_WRAPPER="$REPO/scripts/mlbb_vod_segment_feed.sh"
fi
nohup "$FEED_WRAPPER" >>/root/data/mlbb/mlbb_vod_segment_feed.log 2>&1 &
log "started $FEED_WRAPPER pid=$!"

wait_feed_alive || exit 1
log "=== STEP 3: base feed running until new command ==="
