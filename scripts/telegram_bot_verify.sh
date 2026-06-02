#!/usr/bin/env bash
# Run on VPS to diagnose silent bot.
set -Eeuo pipefail
ENV=/root/.video_bot.env
if [[ ! -f "$ENV" ]]; then
  echo "ERROR: missing $ENV"
  exit 1
fi
set -a
# shellcheck disable=SC1091
source "$ENV"
set +a

echo "=== env (masked) ==="
echo "TG_CHAT_ID=${TG_CHAT_ID:-EMPTY}"
echo "TG_ALLOWED_CHAT_IDS=${TG_ALLOWED_CHAT_IDS:-EMPTY}"
echo "PUBG_CHAT_IDS=${PUBG_CHAT_IDS:-EMPTY}"
echo "BOT script: $(readlink -f /usr/local/bin/telegram_upload_bot.py 2>/dev/null || echo missing)"

echo "=== systemd ==="
systemctl is-active telegram-upload-bot 2>/dev/null || echo "service not active"

echo "=== bot version in script ==="
grep -m1 "BOT_VERSION" /usr/local/bin/telegram_upload_bot.py 2>/dev/null || echo "old script (no BOT_VERSION)"

echo "=== Telegram getMe ==="
curl -fsS "https://api.telegram.org/bot${TG_BOT_TOKEN}/getMe" | head -c 400
echo

echo "=== webhook (must be empty url for polling bot) ==="
curl -fsS "https://api.telegram.org/bot${TG_BOT_TOKEN}/getWebhookInfo"
echo

echo "=== last log lines ==="
tail -15 /root/telegram_upload_bot.log 2>/dev/null || echo "no log"

echo "=== deploy hint ==="
echo "bash /root/content_bot_ml/scripts/deploy_telegram_bot.sh"
echo "# or: cd /root/content_bot_ml && git pull && install -m 755 scripts/{telegram_upload_bot,image_watermark_remove}.py /usr/local/bin/ && systemctl restart telegram-upload-bot"
echo "Then send /ping to the bot from YOUR chat (TG_CHAT_ID=$TG_CHAT_ID)"
