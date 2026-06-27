#!/usr/bin/env bash
# Install nightly YouTube montage cron on VPS (idempotent).
set -Eeuo pipefail
MARK="# youtube-nightly-mlbb"
CRON_LINE="30 1 * * * /usr/local/bin/nightly_youtube.sh"
TMP=$(mktemp)
(crontab -l 2>/dev/null | grep -v "$MARK" | grep -v 'nightly_youtube.sh' || true) >"$TMP"
echo "$CRON_LINE $MARK" >>"$TMP"
crontab "$TMP"
rm -f "$TMP"
echo "Installed: $CRON_LINE"
crontab -l | grep nightly_youtube || true
