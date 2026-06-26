#!/usr/bin/env bash
# Expose VK callback on port 80 (/vk/callback) and optional HTTPS via cloudflared.
set -Eeuo pipefail
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx curl ca-certificates

install -m 644 "$REPO/scripts/vk_nginx_proxy.conf" /etc/nginx/sites-available/vk-callback
ln -sf /etc/nginx/sites-available/vk-callback /etc/nginx/sites-enabled/vk-callback
rm -f /etc/nginx/sites-enabled/default

# Move n8n off public port 80 → localhost:8080 (keep data volume).
if docker ps -a --format '{{.Names}}' | grep -qx 'n8n-new'; then
  docker stop n8n-new >/dev/null 2>&1 || true
  docker rm n8n-new >/dev/null 2>&1 || true
fi
docker run -d \
  --name n8n-new \
  --restart unless-stopped \
  -p 127.0.0.1:8080:5678 \
  -v n8n-data:/home/node/.n8n \
  -e N8N_SECURE_COOKIE=false \
  n8nio/n8n >/dev/null

nginx -t
systemctl enable nginx
systemctl restart nginx
systemctl restart vk-callback-webhook 2>/dev/null || true

PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || echo VPS_IP)
echo "HTTP callback: http://${PUBLIC_IP}/vk/callback"

# cloudflared quick tunnel → HTTPS for VK (required by VK UI).
CF_BIN=/usr/local/bin/cloudflared
if [[ ! -x "$CF_BIN" ]]; then
  curl -fsSL -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
  dpkg -i /tmp/cloudflared.deb || apt-get install -y -f -qq
fi

cat >/etc/systemd/system/vk-cloudflared.service <<'UNIT'
[Unit]
Description=Cloudflare tunnel for VK HTTPS callback
After=network-online.target nginx.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/cloudflared tunnel --url http://127.0.0.1:80 --no-autoupdate
Restart=always
RestartSec=5
StandardOutput=append:/root/data/mlbb/vk_cloudflared.log
StandardError=append:/root/data/mlbb/vk_cloudflared.log

[Install]
WantedBy=multi-user.target
UNIT

mkdir -p /root/data/mlbb
systemctl daemon-reload
systemctl enable vk-cloudflared
systemctl restart vk-cloudflared

sleep 8
HTTPS_URL=$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' /root/data/mlbb/vk_cloudflared.log | tail -1 || true)
if [[ -n "$HTTPS_URL" ]]; then
  echo "HTTPS callback (use in VK): ${HTTPS_URL}/vk/callback"
  echo "${HTTPS_URL}/vk/callback" >/root/data/mlbb/vk_callback_public_url.txt
else
  echo "WARN: cloudflared URL not ready yet — check: tail -f /root/data/mlbb/vk_cloudflared.log"
fi

curl -s -X POST "http://127.0.0.1/vk/callback" \
  -H 'Content-Type: application/json' \
  -d '{"type":"confirmation","group_id":234820335}' || true
echo
