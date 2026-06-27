#!/usr/bin/env bash
# send_video_telegram.sh CHAT_ID CAPTION /path/to/video.mp4
set -Eeuo pipefail
CHAT_ID="${1:?}"
CAPTION="${2:?}"
VIDEO="${3:?}"
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
set -a
# shellcheck disable=SC1091
source /root/.video_bot.env
set +a
curl --noproxy "*" -sS -m 600 \
  -F "chat_id=${CHAT_ID}" \
  -F "video=@${VIDEO}" \
  -F "caption=${CAPTION}" \
  -F "supports_streaming=true" \
  "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendVideo"
