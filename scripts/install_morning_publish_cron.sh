#!/usr/bin/env bash
set -Eeuo pipefail
MARK="# morning-publish-reminder"
LINE="5 9 * * * python3 /usr/local/bin/morning_publish_reminder.py >>/root/data/mlbb/publish/reminder.log 2>&1"
TMP=$(mktemp)
(crontab -l 2>/dev/null | grep -v "$MARK" | grep -v 'morning_publish_reminder' || true) >"$TMP"
echo "$LINE $MARK" >>"$TMP"
crontab "$TMP"
rm -f "$TMP"
echo "Installed: $LINE"
