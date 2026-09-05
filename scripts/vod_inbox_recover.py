#!/usr/bin/env python3
"""Recover PUBG VOD inbox: unpark recent files, clear exhausted flags, skip live stubs."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path


def _game_roots(game: str = "pubg") -> tuple[Path, Path, Path]:
    data = Path(f"/root/data/{game}")
    return data / "youtube_nightly" / "inbox", data / "youtube_nightly" / "parked", data / "vod_segment_state.json"


def unpark_recent(game: str = "pubg", *, limit: int = 5, min_bytes: int = 40_000_000) -> list[str]:
    inbox, parked, _ = _game_roots(game)
    inbox.mkdir(parents=True, exist_ok=True)
    if not parked.exists():
        return []
    moved: list[str] = []
    for src in sorted(parked.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        if src.stat().st_size < min_bytes:
            continue
        dst = inbox / src.name
        if dst.exists():
            continue
        shutil.move(str(src), str(dst))
        moved.append(src.name)
        if len(moved) >= limit:
            break
    return moved


def clear_exhausted(game: str = "pubg", vod_names: list[str] | None = None) -> int:
    _, _, state_path = _game_roots(game)
    if not state_path.exists():
        return 0
    st = json.loads(state_path.read_text(encoding="utf-8"))
    changed = 0
    names = set(vod_names or [])

    def walk(obj):
        nonlocal changed
        if isinstance(obj, dict):
            ident = str(obj.get("id") or obj.get("vod_id") or obj.get("path") or obj.get("file") or "")
            hit = (not names) or any(n.replace("yt_", "").replace(".mp4", "") in ident or n in ident for n in names)
            if hit:
                for key in ("exhausted", "parked_exhausted", "singles_done", "done"):
                    if obj.get(key) in (True, 1, "1", "true"):
                        obj[key] = False
                        changed += 1
                if "reject_reason" in obj and str(obj.get("reject_reason") or "").startswith(
                    ("no_sendable", "singles_exhaust", "presend", "fast_panns")
                ):
                    obj["reject_reason"] = ""
                    changed += 1
                obj["recovered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(st)
    state_path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    return changed


def drop_live_stubs(game: str = "pubg", *, max_bytes: int = 1_000_000) -> list[str]:
    """Remove tiny/corrupt inbox stubs that usually come from live downloads."""
    inbox, _, _ = _game_roots(game)
    removed: list[str] = []
    if not inbox.exists():
        return removed
    for p in list(inbox.glob("yt_*.mp4")):
        try:
            if p.stat().st_size < max_bytes:
                p.unlink(missing_ok=True)
                removed.append(p.name)
        except OSError:
            continue
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--game", default="pubg")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--min-mb", type=float, default=40.0)
    args = ap.parse_args(argv)
    removed = drop_live_stubs(args.game)
    moved = unpark_recent(args.game, limit=args.limit, min_bytes=int(args.min_mb * 1_000_000))
    cleared = clear_exhausted(args.game, moved or None)
    print(json.dumps({"removed_stubs": removed, "unparked": moved, "cleared_fields": cleared}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
