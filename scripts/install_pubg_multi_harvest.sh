#!/usr/bin/env bash
# Install / refresh PUBG multi-platform short harvest (TikTok / IG / VK).
set -euo pipefail
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
cd "$REPO"

# Keep yt-dlp fresh — TikTok downloads need ≥ 2026.08 on this box.
if command -v curl >/dev/null; then
  tmp="$(mktemp)"
  if curl -fsSL "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp" -o "$tmp"; then
    install -m 0755 "$tmp" /usr/local/bin/yt-dlp
  fi
  rm -f "$tmp"
fi
yt-dlp --version || true

install -m 0644 scripts/content-bot-pubg-multi-harvest.service /etc/systemd/system/content-bot-pubg-multi-harvest.service
install -m 0644 scripts/content-bot-pubg-multi-harvest.timer /etc/systemd/system/content-bot-pubg-multi-harvest.timer
systemctl daemon-reload
systemctl enable --now content-bot-pubg-multi-harvest.timer
systemctl start content-bot-pubg-multi-harvest.service || true
echo "multi-harvest installed; timer + one-shot started"
