#!/usr/bin/env bash
# MLBB calibration cron — feed ~25min (Telegram only), ingest ~3h (throttled YouTube).
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
# Sends local Shorts to owner — NO YouTube calls.
set -Eeuo pipefail
set -a
source /root/.video_bot.env
set +a
export CONTENT_BOT_REPO=/root/content_bot_ml
export HIGHLIGHT_HEATMAP=0
export MLBB_CALIBRATION_BATCH=3
export MLBB_FEED_QUIET_EMPTY_SEC=21600
flock -n /tmp/mlbb_calibration_feed.lock \
  python3 /usr/local/bin/mlbb_calibration_feed.py \
  >>/root/data/mlbb/mlbb_calibration_feed.log 2>&1
EOF
chmod 755 "$WRAPPER_FEED"

WRAPPER_INGEST="$BIN/mlbb_youtube_shorts_ingest.sh"
cat >"$WRAPPER_INGEST" <<'EOF'
#!/usr/bin/env bash
# Throttled YouTube ingest — max 3 downloads, 12s pause, skip if queue full.
set -Eeuo pipefail
set -a
source /root/.video_bot.env
set +a
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export CONTENT_BOT_REPO=/root/content_bot_ml
export HIGHLIGHT_HEATMAP=0
export YTDLP_SLEEP_REQUESTS=1.5
export YTDLP_SLEEP_INTERVAL=4
export YTDLP_MAX_SLEEP_INTERVAL=12
export MLBB_INGEST_MAX_DOWNLOADS=3
export MLBB_INGEST_SKIP_IF_PENDING=12
export MLBB_CALIBRATION_LENIENT=1
flock -n /tmp/mlbb_shorts_ingest.lock \
  python3 /usr/local/bin/mlbb_youtube_shorts_ingest.py \
    --incremental --max-downloads 3 --download-delay 12 --search-delay 5 \
  >>/root/data/mlbb/mlbb_shorts_ingest.log 2>&1
EOF
chmod 755 "$WRAPPER_INGEST"

MARK="# mlbb-calibration-cron"
(crontab -l 2>/dev/null | grep -v "$MARK" || true
 echo "*/25 * * * * $WRAPPER_FEED $MARK feed"
 echo "15 */3 * * * $WRAPPER_INGEST $MARK ingest"
 echo "0 5 * * 1 python3 $BIN/mlbb_calibration_weekly_report.py $MARK weekly"
) | crontab -

echo "MLBB calibration cron installed:"
echo "  feed    every 25 min (Telegram only, 3 clips if queue has candidates)"
echo "  ingest  every 3h at :15 UTC — max 3 Shorts, 12s pause, skip if pending>=12"
echo "  report  Monday 08:00 MSK"
