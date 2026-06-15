#!/usr/bin/env bash
# One-shot deploy — steady mode + autonomic recovery.
set -euo pipefail

BIN="/usr/local/bin"
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
ENV="/root/.video_bot.env"

cd "$REPO"
git pull --ff-only origin cursor/content-farm-fixes-1a63 || true
bash "$REPO/deploy/mlbb_deploy.sh"

for kv in \
  MLBB_STEADY_MODE=1 \
  MLBB_STEADY_FEED_INTERVAL_SEC=720 \
  MLBB_STEADY_INGEST_COOLDOWN_SEC=300 \
  MLBB_STEADY_INGEST_MAX_DOWNLOADS=4 \
  MLBB_STEADY_MAX_TIER=2 \
  MLBB_MAX_SILENCE_SEC=5400 \
  MLBB_SHORTS_ONLY=1 \
  MLBB_SHORTS_FOCUS=1 \
  MLBB_SHORTS_MAX_DURATION_SEC=60 \
  MLBB_CALIBRATION_BATCH=4 \
  MLBB_TARGET_PENDING=12 \
  MLBB_SHORTS_CALIBRATION_BURST=0 \
  MLBB_FEED_EMPTY_RUN_SEC=120 \
  MLBB_LEARNING_FIRST=0 \
  MLBB_DISK_INDEX_SEC=90 \
  MLBB_RESCUE_LIMIT=24 \
  MLBB_RESEND_UNLABELED_HOURS=48 \
  MLBB_RESEND_STARVED_HOURS=12 \
  MLBB_ZERO_PENDING_RECOVERY_SEC=900 \
  MLBB_RECOVERY_COOLDOWN_SEC=600 \
  YTDLP_PROXY=; do
  if grep -q "^${key}=" "$ENV" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV"
  else
    echo "${key}=${val}" >> "$ENV"
  fi
done

# Cron: watchdog + health guard every 2 min
CRON_LINE='*/2 * * * * /usr/local/bin/mlbb_continuous_worker_watchdog.sh'
( crontab -l 2>/dev/null | grep -v mlbb_continuous_worker_watchdog || true; echo "$CRON_LINE" ) | crontab -

echo "=== stop heavy jobs ==="
pkill -f mlbb_vod_segment_feed.py 2>/dev/null || true
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
pgrep -af 'mlbb_continuous_worker|youtube_shorts_ingest|calibration_feed' || true
echo "done"
