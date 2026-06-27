#!/usr/bin/env bash
# One-time: AV1/long YouTube VOD -> H.264 720p for faster Smart Edit analysis.
set -Eeuo pipefail
SRC="${1:?usage: youtube_vod_h264_proxy.sh input.mp4 [output.mp4]}"
OUT="${2:-${SRC%.*}_h264.mp4}"
LOG="${OUT%.mp4}_proxy.log"
echo "[$(date -Is)] proxy start $SRC -> $OUT" | tee -a "$LOG"
ffmpeg -y -hide_banner -loglevel error -hwaccel none -i "$SRC" \
  -map 0:v:0 -map 0:a:0? \
  -vf "scale=-2:720" \
  -c:v libx264 -preset ultrafast -crf 23 \
  -c:a aac -b:a 128k \
  -movflags +faststart \
  "$OUT" 2>>"$LOG"
echo "[$(date -Is)] proxy done" | tee -a "$LOG"
