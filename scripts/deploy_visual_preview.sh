#!/usr/bin/env bash
set -Eeuo pipefail
REPO="${REPO:-/root/content_bot_ml}"
BRANCH="${BRANCH:-cursor/mlbb-video-pipeline-e712}"

cd "$REPO"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

install -m 755 \
  scripts/preview_gate.py \
  scripts/highlight_scorer.py \
  scripts/visual_action_check.py \
  scripts/segment_preview.py \
  scripts/pubg_mlbb_pipeline.py \
  scripts/pause_legacy_pipelines.sh \
  scripts/approve_preview_cli.py \
  scripts/strict_montage_direct.py \
  scripts/pubg_brawl_direct.py \
  scripts/smart_video_editor.py \
  scripts/telegram_upload_bot.py \
  scripts/pipeline_watchdog.sh \
  /usr/local/bin/

bash /usr/local/bin/pause_legacy_pipelines.sh
bash "$REPO/scripts/install_pipeline_watchdog_cron.sh"
systemctl restart telegram-upload-bot 2>/dev/null || pkill -f telegram_upload_bot.py; sleep 1; \
  nohup python3 /usr/local/bin/telegram_upload_bot.py >>/root/telegram_upload_bot.log 2>&1 &

nohup /usr/local/bin/run_job_until_ok.sh \
  /root/data/mlbb/pubg_mlbb_pipeline.log \
  python3 /usr/local/bin/pubg_mlbb_pipeline.py --reset \
  >>/root/data/mlbb/pubg_mlbb_pipeline.log 2>&1 &

echo "visual preview pipeline started — tail -f /root/data/mlbb/pubg_mlbb_pipeline.log"
