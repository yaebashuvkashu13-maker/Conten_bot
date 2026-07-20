#!/usr/bin/env bash
# Install systemd unit for telegram_upload_bot (survives reboot, auto-restart).
set -Eeuo pipefail

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
BOT="/usr/local/bin/telegram_upload_bot.py"
ENV_FILE="/root/.video_bot.env"
UNIT=/etc/systemd/system/telegram-upload-bot.service

if [[ ! -f "$BOT" ]]; then
  install -m 755 "$REPO/scripts/telegram_upload_bot.py" "$BOT"
fi

cat >"$UNIT" <<EOF
[Unit]
Description=Telegram upload bot (Conten_bot)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
EnvironmentFile=-${ENV_FILE}
Environment=CONTENT_BOT_REPO=${REPO}
WorkingDirectory=${REPO}
ExecStart=/usr/bin/python3 -u ${BOT}
Restart=on-failure
RestartSec=10
StandardOutput=append:/root/data/mlbb/telegram_upload_bot.log
StandardError=append:/root/data/mlbb/telegram_upload_bot.log

[Install]
WantedBy=multi-user.target
EOF

mkdir -p /root/data/mlbb
systemctl daemon-reload
systemctl enable telegram-upload-bot
systemctl restart telegram-upload-bot
systemctl is-active telegram-upload-bot
echo "OK: telegram-upload-bot.service installed"
