#!/usr/bin/env bash
# One-shot deploy + start mass download (paste-friendly: run as  bash scripts/burst.sh )
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN=/usr/local/bin
mkdir -p /root/data/mlbb /root/datasets/tiktok/mlbb /root/data/pubg /root/datasets/telegram/pubg/stream

for f in \
  tiktok_mass_download.py \
  gameplay_gate.py \
  tiktok_download_batch.py \
  run_parallel_stack.sh \
  tiktok_night_loop.sh \
  tiktok_fast_loop.sh \
  overnight_orchestrator.sh \
  mlbb_popularity.py \
  mlbb_feature_batch.py \
  mlbb_hero_dataset_builder.py \
  pubg_tiktok_gentle.py \
  pubg_stream_learn_worker.py \
  telegram_upload_bot.py \
  youtube_download.py \
  youtube_health_check.py \
  nightly_youtube_montage.py \
  nightly_youtube.sh \
  install_youtube_nightly_cron.sh \
  mlbb_n8n_webhook.py \
  publish_ready_montage.py \
  install_n8n_webhook.sh \
  morning_publish_reminder.py \
  install_morning_publish_cron.sh \
  smart_video_editor.py \
  ad_screenshot_ingest.py; do
  if [[ -f "$ROOT/scripts/$f" ]]; then
    install -m 755 "$ROOT/scripts/$f" "$BIN/$f"
  fi
done

if [[ -f /root/.video_bot.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /root/.video_bot.env
  set +a
fi

if [[ -f "$ROOT/scripts/deploy_telegram_bot.sh" ]]; then
  bash "$ROOT/scripts/deploy_telegram_bot.sh" 2>/dev/null || true
fi
bash "$BIN/run_parallel_stack.sh" 2>/dev/null || bash "$ROOT/scripts/run_parallel_stack.sh"
if ! pgrep -f overnight_orchestrator.sh >/dev/null; then
  nohup bash "$BIN/overnight_orchestrator.sh" >>/root/data/mlbb/overnight_orchestrator.log 2>&1 &
fi

echo "OK burst deploy"
echo -n "mp4 count: "
find /root/datasets/tiktok/mlbb -name '*.mp4' 2>/dev/null | wc -l
echo "log tail:"
tail -5 /root/data/mlbb/mass_download.log 2>/dev/null || true
pgrep -af tiktok_mass || true
