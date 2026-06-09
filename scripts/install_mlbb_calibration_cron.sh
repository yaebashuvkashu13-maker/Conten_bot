#!/usr/bin/env bash
# MLBB calibration cron — feed 3x/day MSK, weekly report Monday, nightly ingest.
set -Eeuo pipefail

REPO="${REPO:-/root/content_bot_ml}"
BIN="/usr/local/bin"

install -m 755 \
  "$REPO/scripts/mlbb_calibration_store.py" \
  "$REPO/scripts/mlbb_youtube_shorts_ingest.py" \
  "$REPO/scripts/mlbb_calibration_feed.py" \
  "$REPO/scripts/mlbb_calibration_weekly_report.py" \
  "$BIN/"

mkdir -p /root/datasets/mlbb/youtube_shorts /root/data/mlbb

WRAPPER_FEED="$BIN/mlbb_calibration_feed.sh"
cat >"$WRAPPER_FEED" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
set -a
source /root/.video_bot.env
set +a
export CONTENT_BOT_REPO=/root/content_bot_ml
export HIGHLIGHT_HEATMAP=0
export MLBB_CALIBRATION_BATCH=5
flock -n /tmp/mlbb_calibration_feed.lock \
  python3 /usr/local/bin/mlbb_calibration_feed.py \
  >>/root/data/mlbb/mlbb_calibration_feed.log 2>&1
EOF
chmod 755 "$WRAPPER_FEED"

WRAPPER_INGEST="$BIN/mlbb_youtube_shorts_ingest.sh"
cat >"$WRAPPER_INGEST" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
set -a
source /root/.video_bot.env
set +a
export CONTENT_BOT_REPO=/root/content_bot_ml
export HIGHLIGHT_HEATMAP=0
flock -n /tmp/mlbb_shorts_ingest.lock \
  python3 /usr/local/bin/mlbb_youtube_shorts_ingest.py --max-per-query 30 \
  >>/root/data/mlbb/mlbb_shorts_ingest.log 2>&1
EOF
chmod 755 "$WRAPPER_INGEST"

MARK="# mlbb-calibration-cron"
(crontab -l 2>/dev/null | grep -v "$MARK" || true
 echo "0 5 * * * $WRAPPER_INGEST $MARK ingest"
 echo "0 6,11,16 * * * $WRAPPER_FEED $MARK feed"
 echo "0 5 * * 1 python3 $BIN/mlbb_calibration_weekly_report.py $MARK weekly"
) | crontab -

echo "MLBB calibration cron installed:"
echo "  ingest  08:00 MSK (05:00 UTC)"
echo "  feed    09:00 / 14:00 / 19:00 MSK"
echo "  report  Monday 08:00 MSK"
