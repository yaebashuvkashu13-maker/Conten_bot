#!/usr/bin/env bash
# MLBB + PUBG + training loop while proxy is active.
set -Eeuo pipefail
LOG=/root/data/mlbb/overnight_orchestrator.log
exec >>"$LOG" 2>&1
export PYTHONPATH=/root/content_bot_ml:/usr/local/bin:${PYTHONPATH:-}
if [[ -f /root/.video_bot.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /root/.video_bot.env
  set +a
fi

PY=python3
REPO=/root/content_bot_ml
BIN=/usr/local/bin

run_step() {
  echo "[$(date)] >>> $*"
  "$@" || echo "[$(date)] step failed: $*"
}

while true; do
  if ! "$PY" "$BIN/proxy_health_check.py"; then
    echo "[$(date)] proxy offline — skip TikTok downloads; local training only"
  else
    run_step "$PY" "$BIN/pubg_tiktok_gentle.py"
  fi
  # MLBB: features + hero folders (download via tiktok_night_loop separately)
  run_step "$PY" "$BIN/mlbb_popularity.py" --rebuild
  run_step "$PY" "$BIN/mlbb_popularity.py" --sync-downloads
  run_step "$PY" "$BIN/mlbb_feature_batch.py"
  run_step "$PY" "$BIN/mlbb_hero_dataset_builder.py"
  # Hayabusa / multi-hero ML if content_bot_ml package exists
  if [[ -d /root/content_bot_ml/content_bot ]]; then
    run_step "$PY" -m content_bot.hero_classifier train \
      --positive-dir /root/hero_datasets/hayabusa \
      --negative-dir /root/datasets/tiktok/mlbb/non_gameplay \
      --output-dir /root/models/mlbb/hayabusa_v1 2>/dev/null || true
  fi
  run_step "$PY" "$BIN/audio_game_extract_worker.py"
  run_step "$PY" "$BIN/ad_screenshot_ingest.py"
  echo "[$(date)] cycle sleep 8m"
  sleep 480
done
