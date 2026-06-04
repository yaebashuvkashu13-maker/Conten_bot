#!/usr/bin/env bash
# Proactive MLBB YouTube: 2h+ VOD → 3 montages, twice daily (no TikTok proxy).
set -Eeuo pipefail
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
DEST=/usr/local/bin
install -m 755 "$REPO/scripts/youtube_triple_montage.py" "$DEST/youtube_triple_montage.py"
install -m 755 "$REPO/scripts/montage_env.py" "$DEST/montage_env.py"
LINE='0 6,14 * * * root unset HTTP_PROXY HTTPS_PROXY ALL_PROXY YTDLP_PROXY; /usr/bin/python3 /usr/local/bin/youtube_triple_montage.py >>/root/data/mlbb/youtube_proactive/cron.log 2>&1 # yt-proactive-mlbb'
mkdir -p /root/data/mlbb/youtube_proactive
if [[ -f /etc/cron.d/mlbb_video ]]; then
  grep -q yt-proactive-mlbb /etc/cron.d/mlbb_video 2>/dev/null || echo "$LINE" >> /etc/cron.d/mlbb_video
else
  echo "$LINE" > /etc/cron.d/youtube_proactive
  chmod 644 /etc/cron.d/youtube_proactive
fi
echo "OK proactive cron (06:00 and 14:00 UTC)"
