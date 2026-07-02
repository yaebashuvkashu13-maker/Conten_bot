#!/usr/bin/env python3
"""
Index ad screenshots forwarded in Telegram (owner saves to ad_examples/).

Later: OCR + template rules for Instagram digest and montage filters.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

AD_DIR = Path("/root/data/mlbb/ad_examples")
INDEX = Path("/root/data/mlbb/ad_examples_index.json")
SUPPORTED = {".jpg", ".jpeg", ".png", ".webp"}


def _meta_note(path: Path) -> str:
    meta = path.with_suffix(".meta.json")
    if not meta.exists():
        meta = path.parent / f"{path.stem}.meta.json"
    if meta.exists():
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
            return f"telegram:{payload.get('chat_id', '?')}"
        except json.JSONDecodeError:
            pass
    return "manual_tg_forward"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def main() -> int:
    AD_DIR.mkdir(parents=True, exist_ok=True)
    known: dict[str, dict] = {}
    if INDEX.exists():
        known = json.loads(INDEX.read_text())

    added = 0
    for path in sorted(AD_DIR.iterdir()):
        if path.suffix.lower() not in SUPPORTED:
            continue
        key = file_hash(path)
        if key in known:
            continue
        known[key] = {
            "path": str(path),
            "name": path.name,
            "size": path.stat().st_size,
            "indexed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": _meta_note(path),
        }
        added += 1

    INDEX.write_text(json.dumps(known, ensure_ascii=False, indent=2))
    print(json.dumps({"total": len(known), "added": added}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
