#!/usr/bin/env bash
# Run ON THE VPS: updates telegram bot + watermark OCR for /wm and IG digest.
set -Eeuo pipefail

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
DEST=/usr/local/bin

cd "$REPO"
git fetch origin cursor/mlbb-video-pipeline-e712 2>/dev/null || git fetch origin
git checkout cursor/mlbb-video-pipeline-e712 2>/dev/null || git pull origin main || true
git pull --ff-only 2>/dev/null || true

apt-get update -qq
apt-get install -y -qq tesseract-ocr curl >/dev/null
python3 -m pip install -q --break-system-packages pytesseract opencv-python-headless numpy PyYAML 2>/dev/null || true

mkdir -p /root/data/mlbb/watermark_examples
install -m 755 "$REPO/scripts/telegram_upload_bot.py" "$DEST/telegram_upload_bot.py"
install -m 755 "$REPO/scripts/image_watermark_remove.py" "$DEST/image_watermark_remove.py"
install -m 755 "$REPO/scripts/instagram_digest_gallery_dl.py" "$DEST/instagram_digest_gallery_dl.py" 2>/dev/null || true
install -m 755 "$REPO/scripts/instagram_digest_run.sh" /usr/local/bin/instagram_digest_run.sh 2>/dev/null || true
install -m 755 "$REPO/scripts/youtube_download.py" "$DEST/youtube_download.py"
install -m 755 "$REPO/scripts/youtube_health_check.py" "$DEST/youtube_health_check.py"
install -m 755 "$REPO/scripts/nightly_youtube_montage.py" "$DEST/nightly_youtube_montage.py"
install -m 755 "$REPO/scripts/nightly_youtube.sh" "$DEST/nightly_youtube.sh"
install -m 755 "$REPO/scripts/install_youtube_nightly_cron.sh" "$DEST/install_youtube_nightly_cron.sh"

# Only one poller — duplicate processes steal getUpdates (HTTP 409).
pkill -f telegram_upload_bot.py 2>/dev/null || true
sleep 2
pgrep -f telegram_upload_bot.py && pkill -9 -f telegram_upload_bot.py 2>/dev/null || true
sleep 1

if ! command -v yt-dlp >/dev/null 2>&1; then
  echo "WARN: yt-dlp not found — YouTube links will fail. Install: pip install -U yt-dlp"
else
  python3 "$DEST/youtube_health_check.py" || echo "WARN: youtube_health_check failed"
fi

if systemctl list-units --type=service 2>/dev/null | grep -q telegram-upload-bot; then
  systemctl restart telegram-upload-bot
  echo "restarted telegram-upload-bot"
else
  nohup python3 "$DEST/telegram_upload_bot.py" >>/root/telegram_upload_bot.log 2>&1 &
  echo "started bot via nohup (no systemd unit)"
fi

grep -m1 BOT_VERSION "$DEST/telegram_upload_bot.py" || true
pgrep -af telegram_upload_bot.py || true
echo "OK. Telegram: /ping (version youtube-v2), /whoami, /yt <url>, or paste Shorts link"
