#!/usr/bin/env bash
# Safe VPS disk cleanup — keeps active/scanned VODs referenced in pipeline state.
set -Eeuo pipefail

STATE=/root/data/mlbb/vod_segment_state.json
INBOX=/root/data/mlbb/youtube_nightly/inbox
KEEP_FILE=/tmp/mlbb_vod_cleanup_keep.txt

echo "BEFORE: $(df -h / | tail -1)"

rm -rf /root/telegram_uploads/pending/*/_yt_tmp_* 2>/dev/null || true
find /root -name '*.part' -delete 2>/dev/null || true
find /root -name '*.f399.mp4' -delete 2>/dev/null || true
find /root -name '*.f299.mp4' -delete 2>/dev/null || true
rm -f /root/data/mlbb/youtube_proactive/iVMOD8v2MRk*.mp4 2>/dev/null || true
find /root/hourly_previews -type f -mtime +3 -delete 2>/dev/null || true
find /root/videos -name '*.mp4' -mtime +2 -size +50M -delete 2>/dev/null || true
find /root/datasets/mlbb/vod_segments -name '*.mp4' -mtime +14 -delete 2>/dev/null || true
find /tmp -maxdepth 1 -name 'mlbb_split_*' -mtime +1 -exec rm -rf {} + 2>/dev/null || true
find /tmp -maxdepth 1 \( -name '*-montage-*' -o -name 'wot-*' -o -name 'pubg-*' -o -name 'standoff-*' \) -mtime +0 -exec rm -rf {} + 2>/dev/null || true
rm -rf /root/.cache/pip /root/.cache/huggingface /root/.cache/yt-dlp 2>/dev/null || true
rm -rf '/root/inbox=' 2>/dev/null || true

# Parks/holds fill the disk and killed the feed for hours (ENOSPC). Keep inbox only.
for g in mlbb pubg standoff genshin wot; do
  base=/root/data/$g/youtube_nightly
  for sub in park_timeout park_dead hold_quota hold_barren exhausted hold; do
    if [[ -d "$base/$sub" ]]; then
      # Keep 2 newest in park_timeout; wipe the rest / all other parks.
      if [[ "$sub" == "park_timeout" ]]; then
        mapfile -t parks < <(ls -t "$base/$sub"/*.mp4 2>/dev/null || true)
        for ((i=2; i<${#parks[@]}; i++)); do
          rm -f "${parks[$i]}"
        done
      else
        rm -rf "$base/$sub"
        mkdir -p "$base/$sub"
      fi
    fi
  done
done

# Old one-shot backups (multi-GB) — never needed for live send.
find /root/data/mlbb/backups -maxdepth 1 -type d -name 'pre_*' -mtime +3 -exec rm -rf {} + 2>/dev/null || true

: >"$KEEP_FILE"
if [[ -f "$STATE" ]]; then
  python3 - "$STATE" "$KEEP_FILE" <<'PY'
import json
import sys
from pathlib import Path

state_path = Path(sys.argv[1])
keep_path = Path(sys.argv[2])
keep: set[str] = set()
try:
    data = json.loads(state_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    data = {}
active = str(data.get("active_vod") or "").strip()
if active:
    keep.add(Path(active).name)
for row in data.get("vods") or []:
    path = str(row.get("path") or "")
    if not path:
        continue
    if not row.get("exhausted"):
        keep.add(Path(path).name)
# scanned_vods history is not kept — exhausted inbox files are safe to delete
keep_path.write_text("\n".join(sorted(keep)) + ("\n" if keep else ""), encoding="utf-8")
PY
fi

# Large feed log slows grep/watchdog.
FEED_LOG=/root/data/mlbb/mlbb_vod_segment_feed.log
if [[ -f "$FEED_LOG" ]]; then
  SZ=$(stat -c %s "$FEED_LOG" 2>/dev/null || echo 0)
  if [[ "$SZ" -gt 50000000 ]]; then
    tail -200000 "$FEED_LOG" >"${FEED_LOG}.tail"
    mv "${FEED_LOG}.tail" "$FEED_LOG"
    echo "trimmed feed log to last 200k lines"
  fi
fi

find /root/data/mlbb/logs -name '*.log' -size +20M -exec truncate -s 5M {} + 2>/dev/null || true

mapfile -t KEEP_INBOX < <(grep -v '^$' "$KEEP_FILE" 2>/dev/null || true)
echo "KEEP inbox files: ${#KEEP_INBOX[@]}"

deleted=0
for f in "$INBOX"/*; do
  [[ -f "$f" ]] || continue
  base=$(basename "$f")
  keep_it=0
  for k in "${KEEP_INBOX[@]}"; do
    if [[ "$base" == "$k" ]]; then
      keep_it=1
      break
    fi
  done
  if [[ "$keep_it" == "1" ]]; then
    continue
  fi
  echo "DEL $f"
  rm -f "$f"
  deleted=$((deleted + 1))
done

echo "Deleted inbox files: $deleted"
echo "AFTER: $(df -h / | tail -1)"
