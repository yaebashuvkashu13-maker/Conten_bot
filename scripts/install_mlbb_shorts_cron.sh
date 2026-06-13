#!/usr/bin/env bash
# MLBB shorts pipeline every 20 minutes (short TikTok/Shorts/Telegram only).
set -Eeuo pipefail

MARK="# mlbb-shorts-pipeline"
LINE="*/20 * * * * /usr/local/bin/run_job_until_ok.sh /root/data/mlbb/mlbb_shorts_pipeline.log python3 /usr/local/bin/mlbb_shorts_pipeline.py --montages 2 >>/root/data/mlbb/mlbb_shorts_pipeline.log 2>&1 $MARK"

TMP=$(mktemp)
(crontab -l 2>/dev/null | grep -v "$MARK" | grep -v 'mlbb_shorts_pipeline.py' || true) >"$TMP"
echo "$LINE" >>"$TMP"
crontab "$TMP"
rm -f "$TMP"

echo "OK mlbb shorts cron every 20 min"
crontab -l | grep mlbb-shorts || true
