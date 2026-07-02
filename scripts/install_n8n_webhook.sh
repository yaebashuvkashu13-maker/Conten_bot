#!/usr/bin/env bash
# Install n8n webhook systemd service on VPS.
set -Eeuo pipefail
DEST=/usr/local/bin
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"

install -m 755 "$REPO/scripts/mlbb_n8n_webhook.py" "$DEST/mlbb_n8n_webhook.py"
install -m 755 "$REPO/scripts/publish_ready_montage.py" "$DEST/publish_ready_montage.py"

if [[ -f /root/.video_bot.env ]] && ! grep -q '^N8N_WEBHOOK_SECRET=' /root/.video_bot.env 2>/dev/null; then
  secret=$(python3 -c "import secrets; print(secrets.token_hex(24))")
  echo "N8N_WEBHOOK_SECRET=$secret" >>/root/.video_bot.env
  echo "Added N8N_WEBHOOK_SECRET to .video_bot.env (copy to n8n credentials)"
fi

grep -q '^N8N_WEBHOOK_PORT=' /root/.video_bot.env 2>/dev/null || echo 'N8N_WEBHOOK_PORT=8787' >>/root/.video_bot.env

cat >/etc/systemd/system/mlbb-n8n-webhook.service <<'UNIT'
[Unit]
Description=MLBB n8n webhook (trigger nightly montage)
After=network.target

[Service]
Type=simple
EnvironmentFile=/root/.video_bot.env
ExecStart=/usr/bin/python3 /usr/local/bin/mlbb_n8n_webhook.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable mlbb-n8n-webhook
systemctl restart mlbb-n8n-webhook
systemctl --no-pager status mlbb-n8n-webhook | head -12
echo "Webhook: http://$(curl -s ifconfig.me 2>/dev/null || echo VPS_IP):8787/health"
echo "Use Authorization: Bearer <N8N_WEBHOOK_SECRET>"
