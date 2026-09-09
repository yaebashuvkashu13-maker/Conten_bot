#!/usr/bin/env bash
# Remove non-PUBG media when VOD_PUBG_ONLY / EU_PUBG_ONLY is active.
# Keeps JSON state, PUBG inbox/parked, PUBG exemplars, caches.
#
#   bash scripts/vod_pipeline_cleanup_non_pubg.sh
set -Eeuo pipefail

if [[ ! -f /root/data/mlbb/EU_PUBG_ONLY ]] && ! grep -q '^VOD_PUBG_ONLY=1' /root/.video_bot.env 2>/dev/null; then
  echo "Refusing: not in PUBG-only mode (set EU_PUBG_ONLY or VOD_PUBG_ONLY=1)" >&2
  exit 1
fi

echo "===== vod_pipeline_cleanup_non_pubg $(date -Is) ====="
df -h / | tail -1

for game in mlbb genshin standoff wot; do
  for sub in inbox parked; do
    dir="/root/data/$game/youtube_nightly/$sub"
    mkdir -p "$dir"
    n=$(find "$dir" -name '*.mp4' 2>/dev/null | wc -l)
    [[ "$n" -eq 0 ]] && continue
    echo "  rm $dir/*.mp4 ($n files, $(du -sh "$dir" | cut -f1))"
    find "$dir" -name '*.mp4' -delete
  done
done

REPO="${REPO:-/root/content_bot_ml}"
for ex in mobile_legends standoff genshin wot; do
  d="$REPO/data/highlight_exemplars/$ex"
  if [[ -d "$d" ]]; then
    echo "  rm $d ($(du -sh "$d" | cut -f1))"
    rm -rf "$d"
  fi
done

for game in mlbb genshin standoff wot; do
  d="/root/datasets/$game"
  if [[ -d "$d" ]]; then
    echo "  rm $d ($(du -sh "$d" | cut -f1))"
    rm -rf "$d"
  fi
done

rm -rf /root/data/mlbb/savage_rescan 2>/dev/null || true
rm -rf /tmp/pubg-montage-* 2>/dev/null || true
find /tmp -maxdepth 1 -name '*.chunk.mp4' -delete 2>/dev/null || true

echo "done"
df -h / | tail -1
