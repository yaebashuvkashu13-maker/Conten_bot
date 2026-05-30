#!/usr/bin/env bash
set -euo pipefail

HERO="${1:?hero required: gusion|lancelot|chou|fanny|hayabusa}"
VIDEO_ROOT="${VIDEO_ROOT:-/workspace/datasets/tiktok/mlbb}"
GAMEPLAY_CSV="${GAMEPLAY_CSV:-/workspace/datasets/tiktok/reports/gameplay_filter_full.csv}"
OUTPUT_DIR="${OUTPUT_DIR:-/workspace/datasets/outputs}"

python3 -m content_bot.montage_builder \
  --hero "$HERO" \
  --video-root "$VIDEO_ROOT" \
  --gameplay-csv "$GAMEPLAY_CSV" \
  --config config.montage.yaml \
  ${SEND_TELEGRAM:+--send-telegram}
