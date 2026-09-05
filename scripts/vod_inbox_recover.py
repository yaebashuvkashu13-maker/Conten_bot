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


# Sticky hard rejects: do not recycle menu/loot VODs without a fresh peak pool.
HARD_BAD_REJECT_MARKERS = (
    "hard_loot",
    "loot_walk",
    "menu_lobby",
    "menu_garage",
    "menu_overlay",
    "hard_menu",
    "presend_menu",
    "run_fake_gun",
)


def _load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _vod_entries(state: dict, name: str) -> list[dict]:
    rows = state.get("vods") or []
    if not isinstance(rows, list):
        return []
    stem = name.replace("yt_", "").replace(".mp4", "")
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ident = str(
            row.get("id")
            or row.get("vod_id")
            or row.get("path")
            or row.get("file")
            or row.get("name")
            or ""
        )
        if name in ident or stem in ident or ident.endswith(name):
            out.append(row)
    return out


def is_hard_bad_without_peaks(state: dict, name: str) -> bool:
    """True when VOD was hard-rejected for menu/loot and has no usable peak pool."""
    for row in _vod_entries(state, name):
        reason = str(row.get("reject_reason") or "").lower()
        if not any(m in reason for m in HARD_BAD_REJECT_MARKERS):
            continue
        peaks = row.get("last_pool_peaks") or row.get("peaks") or []
        if isinstance(peaks, list) and len(peaks) >= 1:
            continue
        return True
    return False


def unpark_recent(
    game: str = "pubg",
    *,
    limit: int = 5,
    min_bytes: int = 40_000_000,
    skip_hard_bad: bool = True,
) -> list[str]:
    inbox, parked, state_path = _game_roots(game)
    inbox.mkdir(parents=True, exist_ok=True)
    if not parked.exists():
        return []
    state = _load_state(state_path)
    moved: list[str] = []
    skipped_hard: list[str] = []
    for src in sorted(parked.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        if src.stat().st_size < min_bytes:
            continue
        dst = inbox / src.name
        if dst.exists():
            continue
        if skip_hard_bad and is_hard_bad_without_peaks(state, src.name):
            skipped_hard.append(src.name)
            continue
        shutil.move(str(src), str(dst))
        moved.append(src.name)
        if len(moved) >= limit:
            break
    if skipped_hard:
        print(
            json.dumps(
                {"unpark_skipped_hard_bad": skipped_hard[:20], "count": len(skipped_hard)},
                ensure_ascii=False,
            ),
            flush=True,
        )
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
                reason = str(obj.get("reject_reason") or "").lower()
                hard_bad = any(m in reason for m in HARD_BAD_REJECT_MARKERS)
                peaks = obj.get("last_pool_peaks") or obj.get("peaks") or []
                has_peaks = isinstance(peaks, list) and len(peaks) >= 1
                # Keep hard menu/loot exhaust sticky until a new peak pool appears.
                if hard_bad and not has_peaks:
                    pass
                else:
                    for key in ("exhausted", "parked_exhausted", "singles_done", "done"):
                        if obj.get(key) in (True, 1, "1", "true"):
                            obj[key] = False
                            changed += 1
                    if "reject_reason" in obj and str(obj.get("reject_reason") or "").startswith(
                        ("no_sendable", "singles_exhaust", "presend", "fast_panns")
                    ):
                        if not hard_bad:
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
    ap.add_argument(
        "--allow-hard-bad",
        action="store_true",
        help="Unpark VODs even if last reject was menu/loot without peaks",
    )
    args = ap.parse_args(argv)
    removed = drop_live_stubs(args.game)
    moved = unpark_recent(
        args.game,
        limit=args.limit,
        min_bytes=int(args.min_mb * 1_000_000),
        skip_hard_bad=not args.allow_hard_bad,
    )
    cleared = clear_exhausted(args.game, moved or None)
    print(json.dumps({"removed_stubs": removed, "unparked": moved, "cleared_fields": cleared}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
