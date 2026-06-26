#!/usr/bin/env bash
# Burst mode: mass TikTok + Instagram prep + game audio — run once when proxy is paid.
set -Eeuo pipefail
mkdir -p /root/data/mlbb/ad_examples /root/datasets/tiktok/mlbb /root/datasets/audio/game_wav
LOG=/root/data/mlbb/parallel_stack.log
exec >>"$LOG" 2>&1
echo "[$(date)] parallel stack start"

export PYTHONPATH=/root:/usr/local/bin:${PYTHONPATH:-}
if [[ -f /root/.video_bot.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /root/.video_bot.env
  set +a
fi

is_running() { pgrep -f "$1" >/dev/null; }

count_mp4() {
  find /root/datasets/tiktok/mlbb -name '*.mp4' 2>/dev/null | wc -l
}

TARGET="${MASS_DOWNLOAD_TARGET:-250}"
WORKERS="${MASS_DOWNLOAD_WORKERS:-2}"
HUMAN="${MASS_HUMAN_MODE:-1}"

# 1) Mass TikTok — gentle by default (human pauses, csv-first)
if ! is_running "tiktok_mass_download.py" && ! is_running "tiktok_night_loop.sh"; then
  ON_DISK="$(count_mp4)"
  echo "mass download: on_disk=$ON_DISK target=$TARGET workers=$WORKERS human=$HUMAN"
  if [[ "$HUMAN" == "1" ]]; then
    nohup bash /usr/local/bin/tiktok_night_loop.sh >>/root/data/mlbb/night_loop.log 2>&1 &
    echo "started tiktok_night_loop pid=$!"
  else
    nohup python3 /usr/local/bin/tiktok_mass_download.py \
      --target "$TARGET" --workers "$WORKERS" \
      >>/root/data/mlbb/mass_download.log 2>&1 &
    echo "started tiktok_mass_download pid=$!"
  fi
fi

# 2) Instagram background (config check; full ingest needs cookies)
if ! is_running "instagram_background_worker.py"; then
  (
    while true; do
      python3 /usr/local/bin/instagram_background_worker.py || true
      sleep 600
    done
  ) >>/root/data/mlbb/instagram_worker.log 2>&1 &
  echo "started instagram worker"
fi

# 3) Game audio wav extraction from downloaded mp4
if ! is_running "audio_game_extract_worker.py"; then
  (
    while true; do
      python3 /usr/local/bin/audio_game_extract_worker.py || true
      sleep 90
    done
  ) >>/root/data/mlbb/audio_worker.log 2>&1 &
  echo "started audio worker"
fi

# 4) Ad screenshot index (owner forwards promos to bot -> save under ad_examples/)
python3 /usr/local/bin/ad_screenshot_ingest.py || true

echo "[$(date)] parallel stack launched on_disk=$(count_mp4)"
