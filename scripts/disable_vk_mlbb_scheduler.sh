#!/usr/bin/env bash
# Disable VK MLBB auto-publish cron + Telegram empty-queue spam.
set -Eeuo pipefail

ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"
MARKERS=("vk-mlbb" "vk_mlbb_publish")

TMP=$(mktemp)
crontab -l 2>/dev/null >"$TMP" || true
for m in "${MARKERS[@]}"; do
  grep -v "$m" "$TMP" >"${TMP}.new" || true
  mv "${TMP}.new" "$TMP"
done
crontab "$TMP"
rm -f "$TMP"

touch "$ENV_FILE"
if ! grep -q '^VK_MLBB_DISABLED=' "$ENV_FILE" 2>/dev/null; then
  echo 'VK_MLBB_DISABLED=1' >>"$ENV_FILE"
else
  sed -i 's/^VK_MLBB_DISABLED=.*/VK_MLBB_DISABLED=1/' "$ENV_FILE"
fi
if ! grep -q '^VK_MLBB_NOTIFY_EMPTY=' "$ENV_FILE" 2>/dev/null; then
  echo 'VK_MLBB_NOTIFY_EMPTY=0' >>"$ENV_FILE"
else
  sed -i 's/^VK_MLBB_NOTIFY_EMPTY=.*/VK_MLBB_NOTIFY_EMPTY=0/' "$ENV_FILE"
fi

echo "OK VK MLBB disabled: cron removed, VK_MLBB_DISABLED=1, notify_empty=0"
crontab -l 2>/dev/null | grep -i vk || echo "(no vk cron lines)"
