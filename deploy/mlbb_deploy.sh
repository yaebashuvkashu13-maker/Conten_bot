#!/usr/bin/env bash
# Deploy MLBB scripts from repo clone to /usr/local/bin
set -euo pipefail

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
BIN="/usr/local/bin"

install -m 755 "$REPO/scripts/mlbb_continuous_worker.py" "$BIN/mlbb_continuous_worker.py"
install -m 755 "$REPO/scripts/mlbb_continuous_worker_watchdog.sh" "$BIN/mlbb_continuous_worker_watchdog.sh"
install -m 755 "$REPO/scripts/mlbb_calibration_feed.py" "$BIN/mlbb_calibration_feed.py"
install -m 755 "$REPO/scripts/mlbb_calibration_store.py" "$BIN/mlbb_calibration_store.py"
install -m 755 "$REPO/scripts/mlbb_youtube_shorts_ingest.py" "$BIN/mlbb_youtube_shorts_ingest.py"
install -m 755 "$REPO/scripts/mlbb_kill_ui.py" "$BIN/mlbb_kill_ui.py"
install -m 644 "$REPO/scripts/mlbb_fight_segment.py" "$BIN/mlbb_fight_segment.py"
install -m 755 "$REPO/scripts/mlbb_force_send_batch.py" "$BIN/mlbb_force_send_batch.py"
install -m 755 "$REPO/scripts/mlbb_force_send_one.py" "$BIN/mlbb_force_send_one.py"
install -m 755 "$REPO/scripts/mlbb_youtube_shorts_ingest.py" "$BIN/mlbb_youtube_shorts_ingest.py"
install -m 755 "$REPO/scripts/mlbb_vod_segment_feed.py" "$BIN/mlbb_vod_segment_feed.py"
install -m 755 "$REPO/scripts/mlbb_vod_segment_store.py" "$BIN/mlbb_vod_segment_store.py"
install -m 755 "$REPO/scripts/mlbb_telegram_handlers.py" "$BIN/mlbb_telegram_handlers.py"
install -m 755 "$REPO/scripts/mlbb_telegram_send.py" "$BIN/mlbb_telegram_send.py"
install -m 755 "$REPO/scripts/mlbb_daily_report.py" "$BIN/mlbb_daily_report.py"
install -m 755 "$REPO/scripts/mlbb_training_archive.py" "$BIN/mlbb_training_archive.py"
install -m 755 "$REPO/scripts/mlbb_purge_legacy_shorts_disk.py" "$BIN/mlbb_purge_legacy_shorts_disk.py"
install -m 755 "$REPO/scripts/mlbb_purge_bad_shorts_queue.py" "$BIN/mlbb_purge_bad_shorts_queue.py"

if [[ "${MLBB_DEPLOY_PURGE_QUEUE:-0}" == "1" ]]; then
  python3 "$BIN/mlbb_purge_bad_shorts_queue.py" || true
fi

echo "deployed MLBB scripts to $BIN"
