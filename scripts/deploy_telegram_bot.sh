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

if systemctl list-units --type=service 2>/dev/null | grep -q telegram-upload-bot; then
  systemctl restart telegram-upload-bot
  echo "restarted telegram-upload-bot"
else
  echo "WARN: start bot manually: python3 $DEST/telegram_upload_bot.py"
fi

grep -m1 BOT_VERSION "$DEST/telegram_upload_bot.py" || true
echo "OK. Send /ping then /wm to @programofloyalbot"
