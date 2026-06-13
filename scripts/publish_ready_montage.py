#!/usr/bin/env python3
"""Register latest montage for n8n / TikTok / YouTube / Instagram upload nodes."""

from __future__ import annotations

import json
import time
from pathlib import Path

MANIFEST = Path("/root/data/mlbb/publish/latest_montage.json")
OUTPUT_DIR = Path("/root/videos")


def register(path: Path, *, source_url: str = "", title: str = "") -> dict:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    payload = {
        "path": str(path),
        "name": path.name,
        "size_mb": round(path.stat().st_size / (1024 * 1024), 2),
        "ready_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_url": source_url,
        "source_title": title[:200],
        "platforms": {
            "tiktok": {"status": "pending", "note": "n8n: Upload-Post / Buffer / manual"},
            "youtube_shorts": {"status": "pending", "note": "n8n: YouTube node + OAuth token"},
            "instagram_reels": {"status": "pending", "note": "n8n: Meta API or manual"},
        },
    }
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def latest_in_output_dir() -> Path | None:
    if not OUTPUT_DIR.exists():
        return None
    files = sorted(OUTPUT_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, help="Montage mp4 path")
    parser.add_argument("--source-url", default="")
    parser.add_argument("--title", default="")
    args = parser.parse_args()
    path = args.path or latest_in_output_dir()
    if not path:
        print("no montage found", flush=True)
        return 1
    data = register(path, source_url=args.source_url, title=args.title)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
