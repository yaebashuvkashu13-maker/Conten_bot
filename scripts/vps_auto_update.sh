#!/usr/bin/env bash
# Cron on VPS: auto git pull + unified production deploy (never Shorts worker / legacy install).
# Example: */15 * * * * /root/content_bot_ml/scripts/vps_auto_update.sh
set -Eeuo pipefail
REPO="${VPS_REPO_PATH:-${CONTENT_BOT_REPO:-/root/content_bot_ml}}"
export UNIFIED_BRANCH="${UNIFIED_BRANCH:-${VOD_DEPLOY_BRANCH:-cursor/vod-unified-production-a016}}"
export CONTENT_BOT_REPO="$REPO"
exec "$REPO/scripts/vps_apply_vod_only.sh"
