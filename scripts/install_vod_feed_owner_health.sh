#!/usr/bin/env bash
# Install feed owner healthcheck cron (every 15 min).
set -euo pipefail
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
SRC="$REPO/scripts/vod_feed_owner_health.py"
BIN="/usr/local/bin/vod_feed_owner_health.py"
WRAPPER="/usr/local/bin/vod_feed_owner_health.sh"
OUT_DIR="${VOD_FEED_HEALTH_LOG_DIR:-/root/data/vod_feed_health}"

mkdir -p "$OUT_DIR"
cp -f "$SRC" "$BIN"
cp -f "$REPO/scripts/vod_clip_quality_ledger.py" /usr/local/bin/ 2>/dev/null || true
cat >"$WRAPPER" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="/usr/local/bin:/root/content_bot_ml/scripts:${PYTHONPATH:-}"
OUT_DIR="${VOD_FEED_HEALTH_LOG_DIR:-/root/data/vod_feed_health}"
mkdir -p "$OUT_DIR"
python3 -u /usr/local/bin/vod_feed_owner_health.py \
  --game "${VOD_FEED_HEALTH_GAME:-pubg}" \
  --env-file "${VOD_BOT_ENV_FILE:-/root/.video_bot.env}" \
  | tee -a "$OUT_DIR/cron.log"
EOF
chmod +x "$WRAPPER" "$BIN"

CRON_LINE="*/15 * * * * $WRAPPER >>$OUT_DIR/cron.log 2>&1"
(crontab -l 2>/dev/null | grep -v 'vod_feed_owner_health' || true; echo "$CRON_LINE") | crontab -
echo "installed feed owner health cron:"
crontab -l | grep vod_feed_owner_health || true
