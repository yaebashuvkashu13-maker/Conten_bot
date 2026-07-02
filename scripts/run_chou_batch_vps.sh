#!/bin/bash
set -euo pipefail
set -a
source /root/.video_bot.env
set +a
export HERO_ID=chou
export MONTAGE_COUNT=7
export OWNER_TRUSTED_SOURCES=1
export STRICT_GAMEPLAY=0
exec python3 -u /usr/local/bin/build_hero_montage_batch.py
