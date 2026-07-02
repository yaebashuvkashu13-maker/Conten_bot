#!/usr/bin/env bash
# Sync VK MLBB token from env → /root/.video_bot.env (run on VPS or via deploy).
set -Eeuo pipefail
ENV=/root/.video_bot.env
TOKEN="${VK_MLBB_ACCESS_TOKEN:-${VK_ACCESS_TOKEN_MLBB:-}}"
if [[ -z "$TOKEN" ]]; then
  echo "SKIP: VK_MLBB_ACCESS_TOKEN not set in environment"
  exit 0
fi
touch "$ENV"
grep -v '^VK_MLBB_ACCESS_TOKEN=' "$ENV" >"${ENV}.tmp" 2>/dev/null || true
mv "${ENV}.tmp" "$ENV"
echo "VK_MLBB_ACCESS_TOKEN=${TOKEN}" >>"$ENV"
chmod 600 "$ENV"
echo "OK VK_MLBB_ACCESS_TOKEN synced ($(wc -c <"$ENV") bytes env file)"
