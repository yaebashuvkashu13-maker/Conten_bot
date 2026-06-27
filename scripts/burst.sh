#!/usr/bin/env bash
# Deploy ML/training helpers — does NOT start competing montage loops (overnight batch owns CPU).
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN=/usr/local/bin
mkdir -p /root/data/mlbb /root/datasets/tiktok/mlbb /root/data/pubg /root/datasets/telegram/pubg/stream

for f in \
  tiktok_mass_download.py \
  gameplay_gate.py \
  tiktok_download_batch.py \
  mlbb_popularity.py \
  mlbb_feature_batch.py \
  mlbb_hero_dataset_builder.py \
  pubg_tiktok_gentle.py \
  pubg_stream_learn_worker.py \
  mlbb_n8n_webhook.py \
  publish_ready_montage.py \
  install_n8n_webhook.sh \
  ad_screenshot_ingest.py; do
  if [[ -f "$ROOT/scripts/$f" ]]; then
    install -m 755 "$ROOT/scripts/$f" "$BIN/$f"
  fi
done

# Primary deploy path (bot + overnight pipeline)
if [[ -f "$ROOT/scripts/deploy_telegram_bot.sh" ]]; then
  bash "$ROOT/scripts/deploy_telegram_bot.sh"
fi

# Optional training loops — off by default during monetization / overnight focus
if [[ "${BURST_ENABLE_TRAINING:-0}" == "1" ]]; then
  for f in run_parallel_stack.sh overnight_orchestrator.sh tiktok_night_loop.sh; do
    [[ -f "$ROOT/scripts/$f" ]] && install -m 755 "$ROOT/scripts/$f" "$BIN/$f"
  done
  bash "$BIN/run_parallel_stack.sh" 2>/dev/null || bash "$ROOT/scripts/run_parallel_stack.sh" || true
  if ! pgrep -f overnight_orchestrator.sh >/dev/null; then
    nohup bash "$BIN/overnight_orchestrator.sh" >>/root/data/mlbb/overnight_orchestrator.log 2>&1 &
  fi
  echo "BURST_ENABLE_TRAINING=1: training loops started"
else
  echo "BURST_ENABLE_TRAINING=0: skipped orchestrator/parallel_stack (overnight batch priority)"
fi

echo "OK burst deploy"
pgrep -af 'overnight_youtube_batch|smart_video_editor|telegram_upload_bot' || true
