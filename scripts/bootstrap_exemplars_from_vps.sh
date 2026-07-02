#!/usr/bin/env bash
# Sync VPS highlight exemplars manifest into git (mp4 stay gitignored).
set -Eeuo pipefail

REPO="${CONTENT_BOT_REPO:-/root/content_bot_ml}"
ROOT="${HIGHLIGHT_EXEMPLAR_ROOT:-/root/data/highlight_exemplars}"
MANIFEST="$REPO/data/highlight_exemplars/manifest.json"

mkdir -p "$(dirname "$MANIFEST")"
python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

repo = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
root = Path(os.environ.get("HIGHLIGHT_EXEMPLAR_ROOT", "/root/data/highlight_exemplars"))
manifest_path = repo / "data" / "highlight_exemplars" / "manifest.json"
out = {"updated_at": datetime.now(timezone.utc).isoformat(), "profiles": {}}
for profile_dir in sorted(root.iterdir()) if root.is_dir() else []:
    if not profile_dir.is_dir():
        continue
    prof = profile_dir.name
    entry = {"good": [], "bad": []}
    for label in ("good", "bad"):
        d = profile_dir / label
        if not d.is_dir():
            continue
        for mp4 in sorted(d.glob("*.mp4")):
            st = mp4.stat()
            entry[label].append(
                {
                    "name": mp4.name,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                }
            )
    out["profiles"][prof] = entry
manifest_path.parent.mkdir(parents=True, exist_ok=True)
manifest_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
print(f"wrote {manifest_path} profiles={len(out['profiles'])}")
PY

cd "$REPO"
if git diff --quiet -- data/highlight_exemplars/manifest.json 2>/dev/null; then
  echo "manifest unchanged"
else
  git add data/highlight_exemplars/manifest.json
  git commit -m "chore: sync highlight exemplars manifest from VPS" || true
fi
