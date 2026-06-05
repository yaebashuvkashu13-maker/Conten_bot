#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
DEST=/usr/local/bin
CRON_FILE=/etc/cron.d/mlbb_video
MARK="# pipeline-watchdog"

install -m 755 "$REPO/scripts/pipeline_watchdog.sh" "$DEST/pipeline_watchdog.sh" 2>/dev/null || true
install -m 755 "$REPO/scripts/run_job_until_ok.sh" "$DEST/run_job_until_ok.sh" 2>/dev/null || true
install -m 755 "$REPO/scripts/pipeline_retry.py" "$DEST/pipeline_retry.py" 2>/dev/null || true

LINE="*/15 * * * * root /usr/local/bin/pipeline_watchdog.sh $MARK"

if [[ -f "$CRON_FILE" ]]; then
  grep -v 'pipeline-watchdog' "$CRON_FILE" > /tmp/mlbb_video.cron || true
else
  : > /tmp/mlbb_video.cron
fi
echo "$LINE" >> /tmp/mlbb_video.cron
install -m 644 /tmp/mlbb_video.cron "$CRON_FILE"
echo "installed pipeline watchdog cron (every 15 min)"
