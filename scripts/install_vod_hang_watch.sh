#!/usr/bin/env bash
# Install systemd timer for vod_hang_detector --tick (sole scheduled healer).
set -euo pipefail
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
UNIT_DIR=/etc/systemd/system
ENV_FILE="${VOD_BOT_ENV_FILE:-/root/.video_bot.env}"

install -m 0755 "$REPO/scripts/vod_hang_detector.py" /usr/local/bin/vod_hang_detector.py
for f in vod_telegram_env.py vod_feed_recover.py vod_force_send.py vod_game_registry.py \
  vod_inbox_recover.py vod_clip_quality_ledger.py; do
  [[ -f "$REPO/scripts/$f" ]] && cp -f "$REPO/scripts/$f" "/usr/local/bin/$f"
done

sed "s|/root/\.video_bot\.env|${ENV_FILE}|g" \
  "$REPO/scripts/content_bot_vod_hang.service" >"$UNIT_DIR/content-bot-vod-hang.service"
install -m 0644 "$REPO/scripts/content_bot_vod_hang.timer" \
  "$UNIT_DIR/content-bot-vod-hang.timer"

systemctl daemon-reload
systemctl enable --now content-bot-vod-hang.timer
systemctl start content-bot-vod-hang.service || true
systemctl is-active content-bot-vod-hang.timer
systemctl list-timers content-bot-vod-hang.timer --no-pager || true
echo "installed content-bot-vod-hang.timer (vod_hang_detector --tick)"
