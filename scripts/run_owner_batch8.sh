#!/usr/bin/env bash
# Owner batch: 8 URLs, double-kill banner, multiple cuts per VOD.
set -Eeuo pipefail
source /root/.video_bot.env
export PYTHONPATH=/root/content_bot_ml/scripts
export MLBB_VOD_ONLY=1
export MLBB_ONLY_MODE=1
export MLBB_VOD_LENIENT_UNIFORM=1
export MLBB_VOD_TAIL_MIN_HUD_RATE=0.45
export MLBB_VOD_SEND_ONE=0
export MLBB_KILL_BANNER_MIN_TIER=double
export HIGHLIGHT_MAX_PANN_PROBE=5
export HIGHLIGHT_MAX_STAGE1=16
export MLBB_VOD_PROBE_LIMIT=16
export MLBB_VOD_SKIP_REVALIDATE=1
exec python3 -u /root/content_bot_ml/scripts/mlbb_vod_oneoff.py --no-resume-worker \
  https://youtu.be/5SGQ9QE98lU \
  https://youtu.be/yx4GHV4MqlA \
  https://youtu.be/N7IRz4QuU94 \
  https://youtu.be/t9cdnBQcsUo \
  https://youtu.be/mjnI7FnjKaA \
  https://youtu.be/gykR44OzMMU \
  https://youtu.be/ZDUkzoeDRAE \
  https://youtu.be/RQI7kMlXfDI
