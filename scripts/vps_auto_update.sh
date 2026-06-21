#!/usr/bin/env bash
# Cron on VPS: auto git pull + VOD-only deploy (never Shorts worker).
# Example: */15 * * * * /root/content_bot_ml/scripts/vps_apply_vod_only.sh
set -Eeuo pipefail
REPO="${VPS_REPO_PATH:-/root/content_bot_ml}"
exec "$REPO/scripts/vps_apply_vod_only.sh"
