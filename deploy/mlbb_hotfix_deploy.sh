#!/usr/bin/env bash
# One-shot deploy — steady Shorts + overnight VOD highlights.
set -euo pipefail

BIN="/usr/local/bin"
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
ENV="/root/.video_bot.env"

cd "$REPO"
git fetch origin cursor/content-farm-fixes-1a63 2>/dev/null || true
BRANCH="origin/cursor/content-farm-fixes-1a63"
for f in mlbb_calibration_store.py mlbb_calibration_feed.py mlbb_continuous_worker.py \
  mlbb_youtube_shorts_ingest.py mlbb_shorts_montage.py mlbb_channel_blocklist.py mlbb_hud_signals.py \
  mlbb_yolo_epic_ui.py mlbb_minimap_analyze.py mlbb_models_download.py mlbb_kill_ui.py \
  mlbb_fight_segment.py mlbb_vod_segment_feed.py mlbb_vod_segment_store.py \
  pubg_ui_refs_download.py mlbb_health_guard.py mlbb_pipeline_health.py mlbb_emergency_prime.py \
  mlbb_telegram_handlers.py mlbb_telegram_send.py \
  youtube_download.py mlbb_learning_first.py gameplay_gate.py; do
  if git show "${BRANCH}:scripts/${f}" >/dev/null 2>&1; then
    git show "${BRANCH}:scripts/${f}" > "$BIN/${f}"
    chmod 755 "$BIN/${f}"
  fi
done
# Do NOT run mlbb_deploy.sh here — server repo may be stale and overwrites /usr/local/bin.

for kv in \
  MLBB_STEADY_MODE=1 \
  MLBB_LEARNING_SPAM_MODE=0 \
  MLBB_LEARNING_FIRST=0 \
  MLBB_SEND_ENABLED=1 \
  MLBB_SHORTS_ONLY=1 \
  MLBB_SHORTS_FOCUS=1 \
  MLBB_VOD_FOCUS=1 \
  MLBB_VOD_PARALLEL=1 \
  MLBB_VOD_DISABLED=0 \
  MLBB_SHORTS_FEED_DURING_VOD=0 \
  MLBB_SHORTS_INGEST_DURING_VOD=0 \
  MLBB_SHORTS_MAX_DURATION_SEC=60 \
  MLBB_SHORTS_CALIBRATION_BURST=0 \
  MLBB_SHORTS_TRIM_OPENING=1 \
  MLBB_SHORTS_MINI_MONTAGE=1 \
  MLBB_SHORTS_TRIM_TAIL=1 \
  MLBB_SHORTS_SEND_MAX_SEC=28 \
  MLBB_SHORTS_SEND_MIN_SEC=8 \
  MLBB_SHORTS_FADE_IN_SEC=0.12 \
  MLBB_SHORTS_FADE_OUT_SEC=0.15 \
  MLBB_REJECT_LATE_ACTION=1 \
  MLBB_CALIBRATION_LENIENT=1 \
  MLBB_CALIBRATION_BATCH=1 \
  MLBB_TARGET_PENDING=6 \
  MLBB_STEADY_FEED_INTERVAL_SEC=14400 \
  MLBB_STEADY_FORCE_SEND_SILENCE_SEC=7200 \
  MLBB_STEADY_MIN_SEND_PENDING=1 \
  MLBB_STEADY_INGEST_COOLDOWN_SEC=300 \
  MLBB_STEADY_INGEST_MAX_DOWNLOADS=2 \
  MLBB_STEADY_INGEST_SEARCH_DELAY=6 \
  MLBB_STEADY_INGEST_QUERIES=1 \
  MLBB_STEADY_MIN_PENDING=2 \
  MLBB_STEADY_STARVATION_SEC=90 \
  MLBB_EMERGENCY_MAX_DOWNLOADS=3 \
  MLBB_EMERGENCY_SKIP_IF_PENDING=2 \
  MLBB_FULL_SWEEP_MIN_PENDING=4 \
  MLBB_STEADY_MAX_TIER=2 \
  MLBB_MAX_SILENCE_SEC=1800 \
  MLBB_ZERO_PENDING_RECOVERY_SEC=120 \
  MLBB_RECOVERY_COOLDOWN_SEC=300 \
  MLBB_WORKER_STALE_SEC=300 \
  MLBB_INGEST_STALE_SEC=480 \
  MLBB_INGEST_MAX_RUN_SEC=600 \
  MLBB_BACKFILL_SENT_AGE_HOURS=0 \
  MLBB_RESEND_STARVED_HOURS=48 \
  MLBB_UNSENDABLE_FEED_RECOVERY=5 \
  MLBB_RETRAIN_MIN_LABELS=5 \
  MLBB_FEED_STALE_SEC=300 \
  MLBB_VOD_MIN_SEC=300 \
  MLBB_VOD_MAX_SEC=1200 \
  MLBB_VOD_TARGET_DUR_SEC=600 \
  MLBB_VOD_MIN_PEAK_SEC=120 \
  MLBB_VOD_LEAD_SEC=4 \
  MLBB_FIGHT_UNTIL_END=1 \
  MLBB_FIGHT_MIN_SEC=10 \
  MLBB_FIGHT_MAX_SEC=90 \
  MLBB_REQUIRE_MULTIKILL=1 \
  MLBB_VOD_CALIBRATION_LENIENT=0 \
  MLBB_KILL_SCAN_SKIP_OCR=0 \
  MLBB_KILL_SCAN_STEP_SEC=30 \
  MLBB_VOD_BATCH_MAX=4 \
  MLBB_VOD_SLICE_MAX_VODS=4 \
  MLBB_VOD_SLICE_MIN=120 \
  MLBB_VOD_MAX_CLIPS_PER_RUN=15 \
  MLBB_VOD_MAX_RUN_SEC=3600 \
  MLBB_VOD_RESET_EXHAUSTED=1 \
  MLBB_VOD_COOLDOWN_SEC=600 \
  MLBB_VOD_PAUSE_WHEN_SHORTS_PENDING=6 \
  MLBB_VOD_PARALLEL_MIN_PENDING=0 \
  MLBB_VOD_PROBE_LIMIT=24 \
  MLBB_FEED_PRUNE_IDENTITY=0 \
  MLBB_FEED_EMPTY_RUN_SEC=180 \
  MLBB_HEALTH_NOTIFY=0 \
  MLBB_MAX_DAILY_SENDS=500 \
  MLBB_DISK_INDEX_SEC=120 \
  MLBB_RESCUE_LIMIT=16 \
  MLBB_BACKFILL_SENT_AT=1 \
  YTDLP_REMOTE_COMPONENTS=ejs:github \
  YTDLP_PLAYER_CLIENTS=web,mweb,tv,android,ios \
  YTDLP_403_RETRY_DELAY=4 \
  YTDLP_PROXY=; do
  key="${kv%%=*}"; val="${kv#*=}"
  if grep -q "^${key}=" "$ENV" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV"
  else
    echo "${key}=${val}" >> "$ENV"
  fi
done

# Disable dead proxy vars (break yt-dlp / curl when inherited).
for key in PROXY_URL HTTP_PROXY HTTPS_PROXY SOCKS5_PROXY; do
  if grep -q "^${key}=" "$ENV" 2>/dev/null; then
    sed -i "s|^${key}=|#${key}=|" "$ENV"
  fi
done

# Cron: watchdog + health guard every 2 min
CRON_LINE='*/2 * * * * /usr/local/bin/mlbb_continuous_worker_watchdog.sh'
( crontab -l 2>/dev/null | grep -v mlbb_continuous_worker_watchdog || true; echo "$CRON_LINE" ) | crontab -

# yt-dlp needs deno + EJS solver for YouTube n-challenge (Jun 2026).
if ! command -v deno >/dev/null 2>&1; then
  apt-get update -qq 2>/dev/null || true
  apt-get install -y -qq unzip curl 2>/dev/null || true
  curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh 2>/dev/null || true
fi

echo "=== stop heavy jobs ==="
pkill -f mlbb_youtube_shorts_ingest.py 2>/dev/null || true
pkill -f mlbb_learn_apply.sh 2>/dev/null || true
pkill -f mlbb_continuous_worker.py 2>/dev/null || true
sleep 2
rm -f /root/data/mlbb/youtube_shorts_ingest.lock /root/data/mlbb/vod_segment_feed.lock /root/data/mlbb/calibration_feed.lock

echo "=== health check ==="
PYTHONPATH="$BIN" python3 "$BIN/mlbb_health_guard.py" --recover || true

echo "=== start worker ==="
nohup python3 "$BIN/mlbb_continuous_worker.py" >> /root/data/mlbb/mlbb_continuous_worker.log 2>&1 &
sleep 5
pgrep -af 'mlbb_continuous_worker|youtube_shorts_ingest|vod_segment_feed|calibration_feed' || true
echo "done"
