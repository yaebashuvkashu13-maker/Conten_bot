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
  pubg_stream_learn_worker.py \
  telegram_upload_bot.py \
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

bash "$BIN/run_parallel_stack.sh" 2>/dev/null || bash "$ROOT/scripts/run_parallel_stack.sh"

echo "OK burst deploy"
echo -n "mp4 count: "
find /root/datasets/tiktok/mlbb -name '*.mp4' 2>/dev/null | wc -l
echo "log tail:"
tail -5 /root/data/mlbb/mass_download.log 2>/dev/null || true
pgrep -af tiktok_mass || true
