#!/usr/bin/env bash
# Morning plan 09:00 MSK, evening report 21:00 MSK (server UTC: 06:00 / 18:00).
set -Eeuo pipefail

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
DEST=/usr/local/bin
MORNING_MARK="# daily-ops-morning"
EVENING_MARK="# daily-ops-evening"
MORNING="0 6 * * * /usr/local/bin/daily_ops_cron.sh morning >>/root/data/mlbb/daily_ops/cron.log 2>&1 $MORNING_MARK"
EVENING="0 18 * * * /usr/local/bin/daily_ops_cron.sh evening >>/root/data/mlbb/daily_ops/cron.log 2>&1 $EVENING_MARK"

install -m 755 "$REPO/scripts/daily_morning_plan.py" "$DEST/daily_morning_plan.py"
install -m 755 "$REPO/scripts/daily_evening_report.py" "$DEST/daily_evening_report.py"
install -m 755 "$REPO/scripts/daily_ops_cron.sh" "$DEST/daily_ops_cron.sh"
mkdir -p /root/data/mlbb/daily_ops

TMP=$(mktemp)
(crontab -l 2>/dev/null | grep -v "$MORNING_MARK" | grep -v "$EVENING_MARK" \
  | grep -v 'daily_ops_cron' || true) >"$TMP"
echo "$MORNING" >>"$TMP"
echo "$EVENING" >>"$TMP"
crontab "$TMP"
rm -f "$TMP"
echo "OK daily ops cron: morning 06:00 UTC, evening 18:00 UTC"
crontab -l | grep daily_ops || true
