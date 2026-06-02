#!/usr/bin/env bash
# Run ON THE VPS from a git checkout or after scp of scripts/.
set -Eeuo pipefail
SRC="${1:-/root/content_bot_ml/scripts}"
DEST=/usr/local/bin
mkdir -p /root/data/mlbb /root/datasets/tiktok/mlbb

for f in \
  tiktok_mass_download.py \
  gameplay_gate.py \
  run_parallel_stack.sh \
  instagram_background_worker.py \
  audio_game_extract_worker.py \
  ad_screenshot_ingest.py \
  telegram_upload_bot.py \
  research_delivery_analysis.py \
  pubg_stream_learn_worker.py \
  tiktok_download_batch.py \
  mlbb_hourly_cycle.sh; do
  install -m 755 "$SRC/$f" "$DEST/$f"
done

echo "Deployed to $DEST"
echo "Start burst: bash $DEST/run_parallel_stack.sh"
echo "Logs: tail -f /root/data/mlbb/mass_download.log /root/data/mlbb/parallel_stack.log"
