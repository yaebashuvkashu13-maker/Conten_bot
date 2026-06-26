#!/usr/bin/env python3
"""Sort MLBB videos into per-hero folders using hashtags + CSV descriptions."""

from __future__ import annotations

import csv
import json
import re
import shutil
import time
from pathlib import Path

HEROES_JSON = Path("/root/content_bot_ml/config/mlbb_heroes.json")
if not HEROES_JSON.exists():
    HEROES_JSON = Path(__file__).resolve().parent.parent / "config/mlbb_heroes.json"
INBOX = Path("/root/datasets/tiktok/mlbb")
OUT_ROOT = Path("/root/hero_datasets")
GAMEPLAY_CSV = Path("/root/data/mlbb/gameplay_filter_latest.csv")
STATE = Path("/root/data/mlbb/hero_sort_state.json")
BATCH = 40


def load_heroes() -> list[dict]:
    data = json.loads(HEROES_JSON.read_text(encoding="utf-8"))
    return data["heroes"]


def hero_from_text(text: str, heroes: list[dict]) -> str | None:
    low = text.lower()
    for hero in heroes:
        for tag in hero["tags"]:
            if re.search(rf"\b{re.escape(tag.lower())}\b", low):
                return hero["id"]
    return None


def csv_descriptions() -> dict[str, str]:
    lookup: dict[str, str] = {}
    if not GAMEPLAY_CSV.exists():
        return lookup
    with GAMEPLAY_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            vid = str(row.get("video_id") or "").strip()
            if vid:
                lookup[vid] = str(row.get("description") or "")
    return lookup


def main() -> int:
    heroes = load_heroes()
    desc = csv_descriptions()
    done = set()
    if STATE.exists():
        done = set(json.loads(STATE.read_text()).get("copied", []))

    moved = 0
    for video in sorted(INBOX.rglob("*.mp4")):
        if "non_gameplay" in video.parts:
            continue
        key = str(video)
        if key in done or moved >= BATCH:
            continue
        vid_match = re.search(r"(\d{10,22})", video.name)
        vid = vid_match.group(1) if vid_match else ""
        text = f"{video.name} {desc.get(vid, '')}"
        hero_id = hero_from_text(text, heroes)
        if not hero_id:
            continue
        dest_dir = OUT_ROOT / hero_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / video.name
        if not dest.exists():
            shutil.copy2(video, dest)
        done.add(key)
        moved += 1

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps(
            {"copied": list(done)[-10000:], "updated": time.strftime("%Y-%m-%d %H:%M:%S"), "last_batch": moved},
            indent=2,
        )
    )
    counts = {h["id"]: len(list((OUT_ROOT / h["id"]).glob("*.mp4"))) for h in heroes if (OUT_ROOT / h["id"]).exists()}
    print(json.dumps({"moved": moved, "counts": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
