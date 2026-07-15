#!/usr/bin/env python3
"""Move ownerphoto_* UI-menu crops out of live positive matchers into ui_template/."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_banner_owner_photo_ingest import banner_ref_root  # noqa: E402
from mlbb_banner_ref_match import clear_banner_ref_cache  # noqa: E402


def migrate() -> dict:
    root = banner_ref_root() / "owner_cal"
    pos = root / "positive"
    dest_dir = root / "ui_template"
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    if not pos.exists():
        return {"moved": 0, "paths": []}
    for path in sorted(pos.rglob("ownerphoto_*.png")):
        dest = dest_dir / path.name
        if dest.exists():
            path.unlink(missing_ok=True)
            moved.append(f"dedupe:{path}")
            continue
        shutil.move(str(path), str(dest))
        moved.append(str(dest))
    try:
        from mlbb_banner_ref_ingest import write_manifest

        write_manifest()
    except Exception:
        pass
    clear_banner_ref_cache()
    return {"moved": len(moved), "ui_dir": str(dest_dir), "sample": moved[:5]}


def main() -> int:
    import json

    print(json.dumps(migrate(), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
