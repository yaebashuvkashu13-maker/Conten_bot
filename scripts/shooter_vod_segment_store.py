#!/usr/bin/env python3
"""Per-game VOD segment store for shooter feeds (PUBG, Standoff)."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from mlbb_vod_segment_store import inline_keyboard_markup, stats as mlbb_stats


def _game_root(game: str) -> Path:
    g = game.strip().lower()
    base = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb")).parent
    return Path(os.environ.get(f"SHOOTER_{g.upper()}_DATA_ROOT", str(base / g)))


def _paths(game: str) -> dict[str, Path]:
    root = _game_root(game)
    return {
        "state": root / "vod_segment_state.json",
        "index": root / "vod_segment_index.json",
        "labels": root / "vod_segment_labels.json",
        "feed_sent": root / "vod_segment_feed_sent.json",
        "inbox": root / "youtube_nightly" / "inbox",
        "segments": Path(
            os.environ.get(
                f"SHOOTER_{game.upper()}_SEGMENTS_ROOT",
                str(Path("/root/datasets") / game / "vod_segments"),
            )
        ),
    }


def vod_youtube_id(path: Path) -> str:
    stem = path.stem
    if stem.startswith("yt_"):
        return stem[3:][:11]
    return stem[:11]


def segment_id(vod_id: str, start_sec: float) -> str:
    return f"{vod_id}_{int(start_sec)}"


def load_index(game: str) -> dict:
    p = _paths(game)["index"]
    if not p.exists():
        return {"segments": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"segments": []}


def load_labels(game: str) -> dict:
    p = _paths(game)["labels"]
    if not p.exists():
        return {"good": [], "bad": [], "feedback": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"good": [], "bad": [], "feedback": []}


def labeled_ids(game: str) -> set[str]:
    data = load_labels(game)
    out: set[str] = set()
    for bucket in ("good", "bad"):
        for row in data.get(bucket, []):
            sid = str(row.get("segment_id", ""))
            if sid:
                out.add(sid)
    for row in data.get("feedback", []):
        sid = str(row.get("segment_id", ""))
        if sid:
            out.add(sid)
    return out


def load_feed_sent(game: str) -> set[str]:
    p = _paths(game)["feed_sent"]
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(x) for x in data.get("sent", [])}
    except (json.JSONDecodeError, OSError):
        return set()


def mark_feed_sent(game: str, segment_ids: list[str]) -> None:
    p = _paths(game)["feed_sent"]
    sent = load_feed_sent(game)
    sent.update(segment_ids)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"sent": sorted(sent)}, indent=2), encoding="utf-8")


def upsert_segment(game: str, row: dict) -> None:
    p = _paths(game)["index"]
    data = load_index(game)
    segments = data.setdefault("segments", [])
    sid = str(row.get("segment_id", ""))
    segments[:] = [s for s in segments if str(s.get("segment_id")) != sid]
    segments.append(row)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def stats(game: str) -> dict:
    data = load_labels(game)
    yes = sum(1 for f in data.get("feedback", []) if f.get("owner_label") in ("yes", "good"))
    no = sum(1 for f in data.get("feedback", []) if f.get("owner_label") in ("no", "bad"))
    return {"feedback_yes": yes, "feedback_no": no, "game": game}


def keyboard(seg_id: str) -> dict:
    return inline_keyboard_markup(seg_id)
