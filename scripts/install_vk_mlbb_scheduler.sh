#!/usr/bin/env bash
# VK MLBB clip publishing: 09:00, 13:30, 18:00 Moscow (06:00, 10:30, 15:00 UTC).
set -Eeuo pipefail
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
DEST=/usr/local/bin

install -m 755 "$REPO/scripts/vk_mlbb_queue.py" "$DEST/"
install -m 755 "$REPO/scripts/vk_mlbb_upload.py" "$DEST/"
install -m 755 "$REPO/scripts/vk_mlbb_publish_slot.py" "$DEST/"
install -m 755 "$REPO/scripts/install_vk_mlbb_scheduler.sh" "$DEST/"

cat >"$DEST/vk_mlbb_publish_slot.sh" <<'WRAP'
#!/usr/bin/env bash
set -Eeuo pipefail
set -a
[[ -f /root/.video_bot.env ]] && source /root/.video_bot.env
set +a
exec python3 /usr/local/bin/vk_mlbb_publish_slot.py "$@"
WRAP
chmod +x "$DEST/vk_mlbb_publish_slot.sh"

mkdir -p /root/data/mlbb/vk_mlbb_queue/pending /root/data/mlbb/vk_mlbb_queue/published

M1="# vk-mlbb-morning"
M2="# vk-mlbb-afternoon"
M3="# vk-mlbb-evening"
TMP=$(mktemp)
(crontab -l 2>/dev/null | grep -v vk-mlbb- | grep -v vk_mlbb_publish || true) >"$TMP"
echo "0 6 * * * /usr/local/bin/vk_mlbb_publish_slot.sh morning >>/root/data/mlbb/vk_mlbb_queue/cron.log 2>&1 $M1" >>"$TMP"
echo "30 10 * * * /usr/local/bin/vk_mlbb_publish_slot.sh afternoon >>/root/data/mlbb/vk_mlbb_queue/cron.log 2>&1 $M2" >>"$TMP"
echo "0 15 * * * /usr/local/bin/vk_mlbb_publish_slot.sh evening >>/root/data/mlbb/vk_mlbb_queue/cron.log 2>&1 $M3" >>"$TMP"
crontab "$TMP"
rm -f "$TMP"
echo "OK VK MLBB cron: 06:00 / 10:30 / 15:00 UTC (09:00 / 13:30 / 18:00 MSK)"
crontab -l | grep vk-mlbb || true
