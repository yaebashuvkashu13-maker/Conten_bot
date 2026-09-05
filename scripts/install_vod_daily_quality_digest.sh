#!/usr/bin/env bash
# Install daily VOD quality digest (Telegram) — 1-day window of weekly reporter.
set -euo pipefail
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
SRC="$REPO/scripts/vod_weekly_quality_report.py"
BIN="/usr/local/bin/vod_weekly_quality_report.py"
WRAPPER="/usr/local/bin/vod_daily_quality_digest.sh"
OUT_DIR="${VOD_QUALITY_REPORT_DIR:-/root/data/vod_quality_reports}"

mkdir -p "$OUT_DIR"
cp -f "$SRC" "$BIN"
cp -f "$REPO/scripts/vod_clip_quality_ledger.py" /usr/local/bin/ 2>/dev/null || true
cat >"$WRAPPER" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="/usr/local/bin:/root/content_bot_ml/scripts:${PYTHONPATH:-}"
OUT_DIR="${VOD_QUALITY_REPORT_DIR:-/root/data/vod_quality_reports}"
mkdir -p "$OUT_DIR"
python3 -u /usr/local/bin/vod_weekly_quality_report.py \
  --days "${VOD_DAILY_DIGEST_DAYS:-1}" \
  --games "${VOD_QUALITY_REPORT_GAMES:-pubg,standoff,wot,mlbb}" \
  --out-dir "$OUT_DIR" \
  --telegram | tee -a "$OUT_DIR/daily_digest.log"
EOF
chmod +x "$WRAPPER" "$BIN" 2>/dev/null || chmod +x "$WRAPPER"

CRON_LINE="20 7 * * * $WRAPPER >>$OUT_DIR/daily_digest.log 2>&1"
(crontab -l 2>/dev/null | grep -v 'vod_daily_quality_digest' || true; echo "$CRON_LINE") | crontab -
echo "installed daily quality digest cron:"
crontab -l | grep vod_daily_quality_digest || true
