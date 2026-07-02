#!/usr/bin/env bash
# Install VK Callback API webhook on VPS.
set -Eeuo pipefail
DEST=/usr/local/bin
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
ENV=/root/.video_bot.env

install -m 755 "$REPO/scripts/vk_callback_webhook.py" "$DEST/vk_callback_webhook.py"

touch "$ENV"
grep -q '^VK_CALLBACK_PORT=' "$ENV" 2>/dev/null || echo 'VK_CALLBACK_PORT=8788' >>"$ENV"
grep -q '^VK_CALLBACK_PATH=' "$ENV" 2>/dev/null || echo 'VK_CALLBACK_PATH=/vk/callback' >>"$ENV"
grep -q '^VK_MLBB_GROUP_ID=' "$ENV" 2>/dev/null || echo 'VK_MLBB_GROUP_ID=234820335' >>"$ENV"
grep -q '^VK_MLBB_CONFIRMATION=' "$ENV" 2>/dev/null || echo 'VK_MLBB_CONFIRMATION=c3de1fe9' >>"$ENV"

# Token from deploy shell env (Cursor secret) — never echo value.
if [[ -n "${VK_MLBB_ACCESS_TOKEN:-}" ]]; then
  if grep -q '^VK_MLBB_ACCESS_TOKEN=' "$ENV" 2>/dev/null; then
    sed -i '/^VK_MLBB_ACCESS_TOKEN=/d' "$ENV"
  fi
  echo "VK_MLBB_ACCESS_TOKEN=${VK_MLBB_ACCESS_TOKEN}" >>"$ENV"
elif [[ -n "${VK_ACCESS_TOKEN_MLBB:-}" ]]; then
  if grep -q '^VK_MLBB_ACCESS_TOKEN=' "$ENV" 2>/dev/null; then
    sed -i '/^VK_MLBB_ACCESS_TOKEN=/d' "$ENV"
  fi
  echo "VK_MLBB_ACCESS_TOKEN=${VK_ACCESS_TOKEN_MLBB}" >>"$ENV"
fi

cat >/etc/systemd/system/vk-callback-webhook.service <<'UNIT'
[Unit]
Description=VK Callback API webhook (MLBB community)
After=network.target

[Service]
Type=simple
EnvironmentFile=/root/.video_bot.env
ExecStart=/usr/bin/python3 /usr/local/bin/vk_callback_webhook.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable vk-callback-webhook
systemctl restart vk-callback-webhook
systemctl --no-pager status vk-callback-webhook | head -12
PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || echo VPS_IP)
echo "VK Callback URL: http://${PUBLIC_IP}:8788/vk/callback"
echo "Health: curl -s http://127.0.0.1:8788/vk/health"
