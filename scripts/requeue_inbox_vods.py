#!/usr/bin/env python3
"""Re-queue downloaded inbox VODs for rescan with current gate logic — no YouTube discovery."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vod_game_registry import VOD_GAMES, inbox_video_ids, load_state, save_state, spec


def _vid_from_path(path: Path) -> str:
    stem = path.stem
    if stem.startswith("yt_"):
        return stem[3:][:11]
    return stem[:11]


def _vid_from_row(row: dict) -> str:
    vid = str(row.get("id") or "")
    if vid:
        return vid
    path = Path(str(row.get("path") or ""))
    if path.name.startswith("yt_"):
        return _vid_from_path(path)
    return ""


def requeue_game(game: str, *, dry_run: bool = False) -> int:
    inbox_ids = inbox_video_ids(game)
    if not inbox_ids:
        print(f"{game}: inbox empty")
        return 0

    state = load_state(game)
    registry = {str(r.get("id") or _vid_from_row(r)): r for r in state.get("vods") or []}
    touched = 0

    for mp4 in sorted(spec(game).inbox().glob("yt_*.mp4")):
        vid = _vid_from_path(mp4)
        if not vid:
            continue
        row = registry.get(vid)
        if row is None:
            row = {"id": vid, "path": str(mp4), "title": "", "exhausted": False}
            state.setdefault("vods", []).append(row)
            registry[vid] = row
        if dry_run:
            print(f"  would requeue {vid} exhausted={row.get('exhausted')}")
        else:
            row["exhausted"] = False
            row["path"] = str(mp4)
            row.pop("reject_reason", None)
            row.pop("last_scan_at", None)
            row.pop("last_scan_blocked", None)
            row.pop("last_pool_peaks", None)
        touched += 1

    if not dry_run and touched:
        save_state(game, state)

    print(f"{game}: {'would requeue' if dry_run else 'requeued'} {touched} inbox VODs for rescan")
    return touched


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-queue inbox VODs for rescan (clear exhaust + scan cache)")
    parser.add_argument("--game", default="pubg", choices=("all", *VOD_GAMES))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    games = list(VOD_GAMES) if args.game == "all" else [args.game]
    total = 0
    for game in games:
        total += requeue_game(game, dry_run=args.dry_run)
    print(f"total requeued={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
