#!/usr/bin/env bash
# MLBB shorts stack sync to VPS /usr/local/bin
set -Eeuo pipefail
REPO="${REPO:-/root/content_bot_ml}"
BRANCH="${BRANCH:-cursor/mlbb-shorts-pipeline-266d}"

cd "$REPO"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "GIT=$(git log -1 --oneline)"

mkdir -p /root/data/mlbb/youtube_shorts/inbox

export MLBB_SHORT_MAX_SEC=180
export MLBB_SHORTS_PER_CYCLE=3
export MLBB_SHORTS_BATCH_SIZE=4
export OWNER_PREVIEW_REQUIRED=0
export MLBB_SHORTS_AUTO_SEND=1

install -m 755 \
  scripts/mlbb_shorts_montage.py \
  scripts/mlbb_shorts_pipeline.py \
  scripts/hourly_new_sources_montage.py \
  scripts/smart_video_editor.py \
  scripts/telegram_upload_bot.py \
  scripts/visual_action_check.py \
  scripts/strict_segment_gate.py \
  scripts/montage_env.py \
  scripts/source_freshness.py \
  scripts/install_mlbb_shorts_cron.sh \
  scripts/pipeline_watchdog.sh \
  /usr/local/bin/

# Stop long-VOD pipelines (MLBB shorts only)
pkill -f 'pubg_mlbb_pipeline.py' 2>/dev/null || true
pkill -f 'overnight_youtube_batch.py' 2>/dev/null || true
if [[ -x /usr/local/bin/pause_legacy_pipelines.sh ]]; then
  bash /usr/local/bin/pause_legacy_pipelines.sh
fi
echo "pubg_mlbb_pipeline.py" >> /root/data/mlbb/PAUSED_PIPELINES 2>/dev/null || true

bash /usr/local/bin/install_mlbb_shorts_cron.sh

systemctl restart telegram-upload-bot 2>/dev/null || true

nohup python3 /usr/local/bin/mlbb_shorts_pipeline.py --montages 3 \
  >>/root/data/mlbb/mlbb_shorts_pipeline.log 2>&1 &

echo "SYNC_OK branch=$BRANCH mlbb_shorts_only"
