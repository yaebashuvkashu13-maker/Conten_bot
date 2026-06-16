#!/usr/bin/env python3
"""Download MLBB YOLO epic UI model to server models dir."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_yolo_epic_ui import download_epic_ui_model, default_model_path


def main() -> int:
    prefer_nano = "--full" not in sys.argv
    path = download_epic_ui_model(prefer_nano=prefer_nano)
    print(
        json.dumps(
            {"ok": True, "path": str(path), "size_mb": round(path.stat().st_size / (1024 * 1024), 2)},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if path.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
