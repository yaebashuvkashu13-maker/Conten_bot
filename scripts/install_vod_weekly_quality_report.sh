#!/usr/bin/env bash
# Install weekly VOD quality report cron + ensure script paths.
set -euo pipefail
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
SRC="$REPO/scripts/vod_weekly_quality_report.py"
BIN="/usr/local/bin/vod_weekly_quality_report.py"
WRAPPER="/usr/local/bin/vod_weekly_quality_report.sh"
OUT_DIR="${VOD_QUALITY_REPORT_DIR:-/root/data/vod_quality_reports}"

mkdir -p "$OUT_DIR"
if [[ -f "$SRC" ]]; then
  cp -f "$SRC" "$BIN"
  cp -f "$REPO/scripts/vod_clip_quality_ledger.py" /usr/local/bin/ 2>/dev/null || true
fi
cat >"$WRAPPER" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="/usr/local/bin:/root/content_bot_ml/scripts:${PYTHONPATH:-}"
OUT_DIR="${VOD_QUALITY_REPORT_DIR:-/root/data/vod_quality_reports}"
mkdir -p "$OUT_DIR"
python3 -u /usr/local/bin/vod_weekly_quality_report.py \
  --days "${VOD_QUALITY_REPORT_DAYS:-7}" \
  --games "${VOD_QUALITY_REPORT_GAMES:-pubg,standoff,wot,mlbb}" \
  --out-dir "$OUT_DIR" | tee -a "$OUT_DIR/cron.log"
EOF
chmod +x "$WRAPPER" "$BIN" 2>/dev/null || chmod +x "$WRAPPER"

CRON_LINE="15 6 * * 1 $WRAPPER >>$OUT_DIR/cron.log 2>&1"
(crontab -l 2>/dev/null | grep -v 'vod_weekly_quality_report' || true; echo "$CRON_LINE") | crontab -
echo "installed weekly quality report cron:"
crontab -l | grep vod_weekly_quality_report || true
