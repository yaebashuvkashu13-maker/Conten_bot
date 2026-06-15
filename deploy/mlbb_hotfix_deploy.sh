#!/usr/bin/env bash
# One-shot deploy after git pull — fixes empty Shorts queue + restarts worker.
set -euo pipefail

BIN="/usr/local/bin"
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
ENV="/root/.video_bot.env"

cd "$REPO"
git pull --ff-only origin cursor/content-farm-fixes-1a63 || true
bash "$REPO/deploy/mlbb_deploy.sh"

for kv in \
  MLBB_SHORTS_ONLY=1 \
  MLBB_SHORTS_FOCUS=1 \
  MLBB_SHORTS_MAX_DURATION_SEC=60 \
  MLBB_CALIBRATION_BATCH=6 \
  MLBB_FEED_COOLDOWN_SEC=90 \
  MLBB_FEED_COOLDOWN_PENDING_SEC=60 \
  MLBB_FEED_EMPTY_RUN_SEC=60 \
  MLBB_LEARNING_FIRST=0 \
  MLBB_STARVATION_PENDING=3 \
  MLBB_STARVATION_INGEST_SEC=120 \
  MLBB_DISK_INDEX_SEC=60 \
  MLBB_RESCUE_LIMIT=24 \
  MLBB_RESEND_UNLABELED_HOURS=48; do
  key="${kv%%=*}"; val="${kv#*=}"
  if grep -q "^${key}=" "$ENV" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV"
  else
    echo "${key}=${val}" >> "$ENV"
  fi
done

echo "=== stop heavy jobs ==="
pkill -f mlbb_vod_segment_feed.py 2>/dev/null || true
pkill -f mlbb_youtube_shorts_ingest.py 2>/dev/null || true
pkill -f mlbb_learn_apply.sh 2>/dev/null || true
pkill -f mlbb_continuous_worker.py 2>/dev/null || true
sleep 2
rm -f /root/data/mlbb/youtube_shorts_ingest.lock /root/data/mlbb/vod_segment_feed.lock /root/data/mlbb/calibration_feed.lock

echo "=== index disk shorts ==="
PYTHONPATH="$BIN" python3 -c "
from mlbb_calibration_store import index_unlabeled_disk_shorts, pending_candidates
n = index_unlabeled_disk_shorts(limit=32)
print('indexed', n, 'pending', len(pending_candidates(limit=99)))
"

echo "=== start worker ==="
nohup python3 "$BIN/mlbb_continuous_worker.py" >> /root/data/mlbb/mlbb_continuous_worker.log 2>&1 &
sleep 5
pgrep -af 'mlbb_continuous_worker|youtube_shorts_ingest|calibration_feed' || true

echo "=== force feed if pending ==="
PYTHONPATH="$BIN" MLBB_FEED_REBUILD=1 python3 "$BIN/mlbb_calibration_feed.py" || true
echo "done"
