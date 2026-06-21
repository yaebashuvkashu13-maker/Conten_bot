#!/usr/bin/env bash
# Sync MLBB scripts to /usr/local/bin and restart services. Run on VPS after git pull.
set -euo pipefail
REPO="${REPO:-/root/content_bot_ml}"
BIN=/usr/local/bin
shopt -s nullglob
for f in \
  mlbb_calibration_store.py \
  mlbb_calibration_feed.py \
  mlbb_continuous_worker.py \
  mlbb_youtube_shorts_ingest.py \
  mlbb_hero_shorts_montage.py \
  mlbb_telegram_video.py \
  mlbb_job_watchdog.py \
  mlbb_owner_learning.py \
  mlbb_correspondence.py \
  mlbb_shorts_title_gate.py \
  mlbb_vod_segment_feed.py \
  mlbb_vod_segment_store.py \
  mlbb_fight_segment.py \
  youtube_mlbb_vod_prefs.py \
  mlbb_vod_only_verify.sh \
  gameplay_gate.py \
  telegram_upload_bot.py \
  mlbb_runtime_cleanup.py \
  mlbb_continuous_worker_watchdog.sh; do
  if [[ -f "$REPO/scripts/$f" ]]; then
    install -m 755 "$REPO/scripts/$f" "$BIN/$f"
  fi
done
if [[ -f "$REPO/scripts/mlbb_learn_apply.sh" ]]; then
  install -m 755 "$REPO/scripts/mlbb_learn_apply.sh" "$BIN/mlbb_learn_apply.sh"
fi
if [[ -f "$REPO/scripts/highlight_train.py" ]]; then
  install -m 755 "$REPO/scripts/highlight_train.py" "$BIN/highlight_train.py"
fi
if [[ -f "$REPO/scripts/highlight_scorer.py" ]]; then
  install -m 755 "$REPO/scripts/highlight_scorer.py" "$BIN/highlight_scorer.py"
fi
echo "synced $(date -u +%Y-%m-%dT%H:%M:%SZ)"
