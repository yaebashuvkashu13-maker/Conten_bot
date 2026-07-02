#!/usr/bin/env bash
# 24/7 MLBB pipeline — replaces sparse cron (feed 25m, ingest 3h, vod hourly).
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
BIN=/usr/local/bin
MARK="# mlbb-continuous-worker"

install -m 755 \
  "$REPO/scripts/mlbb_continuous_worker.py" \
  "$REPO/scripts/mlbb_continuous_worker_watchdog.sh" \
  "$REPO/scripts/mlbb_job_watchdog.py" \
  "$REPO/scripts/mlbb_vod_segment_feed.py" \
  "$REPO/scripts/mlbb_fight_segment.py" \
  "$REPO/scripts/mlbb_learning_first.py" \
  "$REPO/scripts/mlbb_calibration_feed.py" \
  "$REPO/scripts/mlbb_calibration_store.py" \
  "$REPO/scripts/mlbb_youtube_shorts_ingest.py" \
  "$REPO/scripts/mlbb_hero_shorts_montage.py" \
  "$REPO/scripts/mlbb_runtime_cleanup.py" \
  "$REPO/scripts/mlbb_telegram_video.py" \
  "$REPO/scripts/mlbb_vod_montage_feed.py" \
  "$REPO/scripts/mlbb_viral_threshold_sync.py" \
  "$REPO/scripts/mlbb_daily_report.py" \
  "$BIN/"

# Stop sparse cron jobs — worker owns the loop now
TMP=$(mktemp)
crontab -l 2>/dev/null | grep -v "$MARK" \
  | grep -v 'mlbb-calibration-cron' \
  | grep -v 'mlbb_calibration_feed.sh' \
  | grep -v 'mlbb_youtube_shorts_ingest.sh' \
  | grep -v 'mlbb-vod-segment-cron' \
  | grep -v 'mlbb_vod_segment_feed.sh' \
  | grep -v 'mlbb_continuous_worker_watchdog' \
  >"$TMP" || true
echo "*/2 * * * * $BIN/mlbb_continuous_worker_watchdog.sh >>/root/data/mlbb/logs/mlbb_continuous_watchdog.log 2>&1 $MARK watchdog2" >>"$TMP"
crontab "$TMP"
rm -f "$TMP"

pkill -f mlbb_continuous_worker.py 2>/dev/null || true
sleep 1
set -a
# shellcheck disable=SC1091
source /root/.video_bot.env 2>/dev/null || true
set +a
nohup python3 "$BIN/mlbb_continuous_worker.py" >>/root/data/mlbb/mlbb_continuous_worker.log 2>&1 &

echo "OK mlbb continuous worker started (watchdog every 5 min)"
pgrep -af mlbb_continuous_worker || true
