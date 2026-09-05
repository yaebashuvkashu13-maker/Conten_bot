#!/usr/bin/env bash
# Install drought watcher cron (every 30 min).
set -euo pipefail
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
SRC="$REPO/scripts/vod_send_drought_watch.py"
BIN="/usr/local/bin/vod_send_drought_watch.py"
WRAPPER="/usr/local/bin/vod_send_drought_watch.sh"
OUT_DIR="${VOD_DROUGHT_LOG_DIR:-/root/data/vod_drought_watch}"

mkdir -p "$OUT_DIR"
cp -f "$SRC" "$BIN"
cp -f "$REPO/scripts/vod_inbox_recover.py" /usr/local/bin/ 2>/dev/null || true
cp -f "$REPO/scripts/vod_hang_detector.py" /usr/local/bin/ 2>/dev/null || true
cat >"$WRAPPER" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="/usr/local/bin:/root/content_bot_ml/scripts:${PYTHONPATH:-}"
OUT_DIR="${VOD_DROUGHT_LOG_DIR:-/root/data/vod_drought_watch}"
mkdir -p "$OUT_DIR"
python3 -u /usr/local/bin/vod_send_drought_watch.py \
  --game "${VOD_DROUGHT_GAME:-pubg}" \
  --hours "${VOD_DROUGHT_HOURS:-2}" | tee -a "$OUT_DIR/cron.log"
EOF
chmod +x "$WRAPPER" "$BIN"

CRON_LINE="*/30 * * * * $WRAPPER >>$OUT_DIR/cron.log 2>&1"
(crontab -l 2>/dev/null | grep -v 'vod_send_drought_watch' || true; echo "$CRON_LINE") | crontab -
echo "installed drought watch cron:"
crontab -l | grep vod_send_drought_watch || true
