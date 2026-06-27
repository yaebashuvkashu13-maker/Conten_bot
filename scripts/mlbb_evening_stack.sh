#!/usr/bin/env bash
# Evening stack: sort heroes + build 1-2 single-hero montages (game audio only).
set -Eeuo pipefail
export PYTHONPATH=/usr/local/bin:${PYTHONPATH:-}
if [[ -f /root/.video_bot.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /root/.video_bot.env
  set +a
fi

LOG=/root/data/mlbb/evening_stack.log
exec >>"$LOG" 2>&1
echo "[$(date)] evening stack start"

python3 /usr/local/bin/mlbb_hero_dataset_builder.py || true

export SMART_ADD_MUSIC=0
export MLBB_EVENING_COUNT="${MLBB_EVENING_COUNT:-2}"
export MLBB_EVENING_THEME="${MLBB_EVENING_THEME:-Hero Highlights}"
export BLUR_NICKNAME="${BLUR_NICKNAME:-0}"
export STRICT_GAMEPLAY="${STRICT_GAMEPLAY:-1}"
export SMART_MAX_OVERLAY_TEXT="${SMART_MAX_OVERLAY_TEXT:-0.28}"
export SMART_MAX_REJECT_SIM="${SMART_MAX_REJECT_SIM:-0.76}"
export SMART_MIN_HUD_FRAME_RATE="${SMART_MIN_HUD_FRAME_RATE:-0.60}"

python3 /usr/local/bin/mlbb_evening_hero_montages.py
echo "[$(date)] evening stack done"
