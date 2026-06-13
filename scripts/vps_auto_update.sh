#!/usr/bin/env bash
# Cron on VPS: auto git pull + deploy + burst (no manual console).
# Example cron (every 30 min): */30 * * * * /root/content_bot_ml/scripts/vps_auto_update.sh >>/root/data/mlbb/auto_update.log 2>&1
set -Eeuo pipefail
REPO="${VPS_REPO_PATH:-/root/content_bot_ml}"
LOG=/root/data/mlbb/auto_update.log
mkdir -p /root/data/mlbb
exec >>"$LOG" 2>&1
echo "[$(date)] auto_update start"
cd "$REPO" || exit 1
git fetch origin
git checkout cursor/mlbb-video-pipeline-e712 2>/dev/null || git checkout main
git pull --ff-only
# deploy only — burst training loops off by default (BURST_ENABLE_TRAINING=0)
bash "$REPO/scripts/deploy_telegram_bot.sh"
echo "[$(date)] auto_update done"
