#!/usr/bin/env bash
# Hourly MLBB pipeline: download gameplay TikToks, run Smart Edit, send progress.
set -Eeuo pipefail
LOG_FILE="/root/data/mlbb/hourly_cycle.log"
mkdir -p /root/data/mlbb "$(dirname "$LOG_FILE")"
exec >>"$LOG_FILE" 2>&1
printf '\n[%s] hourly cycle start\n' "$(date '+%Y-%m-%d %H:%M:%S')"

export PYTHONPATH=/root:/usr/local/bin:${PYTHONPATH:-}
if [[ -f /root/.video_bot.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /root/.video_bot.env
  set +a
fi

# During paid-proxy burst, skip small hourly batch if mass downloader is running
if pgrep -f tiktok_mass_download.py >/dev/null; then
  echo "mass download active — skip tiktok_download_batch"
else
  /usr/bin/python3 /usr/local/bin/tiktok_download_batch.py --limit 45 || true
fi
/usr/bin/python3 /usr/local/bin/hourly_new_sources_montage.py || true
/usr/bin/python3 /usr/local/bin/mlbb_progress_report.py --attach-latest-video || true

printf '[%s] hourly cycle done\n' "$(date '+%Y-%m-%d %H:%M:%S')"
