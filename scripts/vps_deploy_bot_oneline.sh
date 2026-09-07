#!/usr/bin/env bash
# Paste on VPS (one block): updates bot + kills duplicate processes + restart.
set -Eeuo pipefail
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
cd "$REPO" && git fetch origin cursor/vod-unified-production-a016 && git checkout cursor/vod-unified-production-a016 && git pull --ff-only
bash "$REPO/scripts/deploy_telegram_bot.sh"
pgrep -af telegram_upload_bot.py || true
command -v yt-dlp && python3 /usr/local/bin/youtube_health_check.py || echo "INSTALL: apt install yt-dlp OR pip install yt-dlp"
