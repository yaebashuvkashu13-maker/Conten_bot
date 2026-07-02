#!/usr/bin/env bash
# Download owner VODs for MLBB, Genshin, WoT into nightly inbox.
set -Eeuo pipefail

INBOX="${HIGHLIGHT_INBOX:-/root/data/mlbb/youtube_nightly/inbox}"
DL="${YOUTUBE_DOWNLOAD:-/usr/local/bin/youtube_download.py}"
LOG="${1:-/root/data/mlbb/download_three_game_vods.log}"
mkdir -p "$INBOX"
exec >>"$LOG" 2>&1

download() {
  local name="$1"
  local url="$2"
  local out="$INBOX/yt_${name}.mp4"
  if [[ -f "$out" ]] && [[ "$(stat -c%s "$out" 2>/dev/null || echo 0)" -gt 50000000 ]]; then
    echo "[$(date -Is)] skip $name — already have $(du -h "$out" | cut -f1)"
    return 0
  fi
  echo "[$(date -Is)] download $name $url"
  python3 "$DL" "$url" --out "$INBOX" || echo "[$(date -Is)] FAIL $name"
}

echo "[$(date -Is)] start three-game VOD download"
download "E4Dsp53yvv4" "https://www.youtube.com/watch?v=E4Dsp53yvv4"
download "i67K34fQa9I" "https://www.youtube.com/watch?v=i67K34fQa9I"
download "QbBwJJTio6A" "https://www.youtube.com/watch?v=QbBwJJTio6A"
echo "[$(date -Is)] done"
