#!/usr/bin/env bash
# Cut good exemplar clips from owner labels for MLBB, Genshin, WoT.
set -Eeuo pipefail

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
INBOX="/root/data/mlbb/youtube_nightly/inbox"
BOOT="${HIGHLIGHT_BOOTSTRAP:-/usr/local/bin/highlight_bootstrap_exemplars.py}"
export CONTENT_BOT_REPO="$REPO"
export HIGHLIGHT_EXEMPLAR_ROOT="${HIGHLIGHT_EXEMPLAR_ROOT:-$REPO/data/highlight_exemplars}"

run_game() {
  local game="$1"
  local vod="$2"
  if [[ ! -f "$INBOX/$vod" ]]; then
    echo "skip $game — missing $vod"
    return 1
  fi
  python3 "$BOOT" --game "$game" --vod "$vod"
}

run_game mobile_legends yt_E4Dsp53yvv4.mp4 || true
run_game genshin yt_i67K34fQa9I.mp4 || true
run_game wot yt_QbBwJJTio6A.mp4 || true

for g in mobile_legends genshin wot; do
  n=$(ls "$HIGHLIGHT_EXEMPLAR_ROOT/$g/good" 2>/dev/null | wc -l)
  echo "exemplars $g good=$n"
done
