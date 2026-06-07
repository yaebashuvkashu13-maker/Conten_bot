#!/usr/bin/env bash
# Safe VPS disk cleanup — keeps active pipeline VODs.
set -Eeuo pipefail

KEEP_INBOX=(
  yt_pJ-X6NdSU9k.mp4
  yt_zv3JymSZOb0.mp4
  yt_FpMs48XOnq0.mp4
  yt_n97cHIR9Qow.mp4
  yt_z8ImUR0_x_M.mp4
)

echo "BEFORE: $(df -h / | tail -1)"

rm -rf /root/telegram_uploads/pending/*/_yt_tmp_* 2>/dev/null || true
find /root -name '*.part' -delete 2>/dev/null || true
find /root -name '*.f399.mp4' -delete 2>/dev/null || true
find /root -name '*.f299.mp4' -delete 2>/dev/null || true
rm -f /root/data/mlbb/youtube_proactive/iVMOD8v2MRk*.mp4 2>/dev/null || true
find /root/hourly_previews -type f -mtime +3 -delete 2>/dev/null || true
find /root/videos -name '*.mp4' -mtime +2 -size +50M -delete 2>/dev/null || true
rm -rf /root/.cache/pip /root/.cache/huggingface 2>/dev/null || true

INBOX=/root/data/mlbb/youtube_nightly/inbox
for f in "$INBOX"/*; do
  [[ -f "$f" ]] || continue
  base=$(basename "$f")
  for k in "${KEEP_INBOX[@]}"; do
    [[ "$base" == "$k" ]] && continue 2
  done
  echo "DEL $f"
  rm -f "$f"
done

echo "AFTER: $(df -h / | tail -1)"
