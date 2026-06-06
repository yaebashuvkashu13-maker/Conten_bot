#!/usr/bin/env bash
set -Eeuo pipefail
REPO="${REPO:-/root/content_bot_ml}"
MARK="# pipeline-watchdog"
LINE="*/15 * * * * bash /usr/local/bin/pipeline_watchdog.sh"
install -m 755 "$REPO/scripts/pipeline_watchdog.sh" /usr/local/bin/pipeline_watchdog.sh
TMP=$(mktemp)
(crontab -l 2>/dev/null | grep -v "$MARK" | grep -v 'pipeline_watchdog' || true) >"$TMP"
echo "$LINE $MARK" >>"$TMP"
crontab "$TMP"
rm -f "$TMP"
echo "OK pipeline_watchdog every 15 min"
