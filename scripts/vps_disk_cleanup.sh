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
rm -rf /root/.cache/pip /root/.cache/huggingface 2>/dev/null || true

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
for name in data.get("scanned_vods") or []:
    keep.add(Path(str(name)).name)
keep_path.write_text("\n".join(sorted(keep)) + ("\n" if keep else ""), encoding="utf-8")
PY
fi

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
