#!/usr/bin/env bash
# Daily MLBB Instagram blogger digest -> owner Telegram. Low impact vs Smart Edit/ffmpeg.
set -Eeuo pipefail

LOCK=/var/lock/instagram_digest.lock
LOG=/root/data/mlbb/instagram_digest.log
REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
ENV_FILE="${ENV_FILE:-/root/.video_bot.env}"

exec >>"$LOG" 2>&1
echo "[$(date -Is)] instagram digest start pid=$$"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -Is)] skip: another digest run is active"
  exit 0
fi

export IG_CONFIG_OUT=/root/config.instagram-mlbb.yaml
export IG_CONFIG_TEMPLATE="$REPO/config.instagram-mlbb.yaml"
export IG_DIGEST_MAX_POSTS="${IG_DIGEST_MAX_POSTS:-7}"
export IG_DIGEST_MAX_PER_SOURCE="${IG_DIGEST_MAX_PER_SOURCE:-2}"
export IG_DIGEST_DRY_RUN="${IG_DIGEST_DRY_RUN:-0}"

python3 "$REPO/scripts/build_instagram_config.py" || exit 1

if [[ -n "${INSTAGRAM_COOKIES_PATH:-}" && ! -f "${INSTAGRAM_COOKIES_PATH}" ]]; then
  echo "[$(date -Is)] WARN: cookies missing at $INSTAGRAM_COOKIES_PATH — IG may block"
fi

cd "$REPO"
python3 -m content_bot.main --config /root/config.instagram-mlbb.yaml
rc=$?
echo "[$(date -Is)] instagram digest done rc=$rc"
exit "$rc"
