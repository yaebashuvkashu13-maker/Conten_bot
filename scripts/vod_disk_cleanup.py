#!/usr/bin/env python3
"""Watermark-based VOD cleanup that preserves active and owner-labeled sources."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


GAMES = ("mlbb", "pubg", "standoff", "genshin", "wot")


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    priority: int
    size: int
    mtime: float
    reason: str


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _video_id(path: Path) -> str:
    stem = path.stem
    if stem.startswith("yt_"):
        return stem[3:14]
    return ""


def _owner_labeled_ids(repo_root: Path, data_root: Path) -> set[str]:
    ids: set[str] = set()
    paths = list((repo_root / "data").glob("*owner_labels.json"))
    paths.extend(data_root.glob("*/*owner_labels.json"))
    paths.extend(data_root.glob("mlbb/*owner_labels.json"))
    for path in paths:
        videos = _read_json(path).get("videos")
        if isinstance(videos, dict):
            ids.update(str(video_id) for video_id in videos if video_id)
    return ids


def _state_keep_names(data_root: Path, game: str) -> tuple[set[str], set[str]]:
    state = _read_json(data_root / game / "vod_segment_state.json")
    keep: set[str] = set()
    exhausted: set[str] = set()
    active = str(state.get("active_vod") or "")
    if active:
        keep.add(Path(active).name)
    for row in state.get("vods") or []:
        if not isinstance(row, dict):
            continue
        name = Path(str(row.get("path") or "")).name
        if not name:
            continue
        if row.get("exhausted"):
            exhausted.add(name)
        else:
            keep.add(name)
    return keep, exhausted


def _open_paths(proc_root: Path = Path("/proc")) -> set[Path]:
    paths: set[Path] = set()
    for fd_dir in proc_root.glob("[0-9]*/fd"):
        try:
            links = list(fd_dir.iterdir())
        except OSError:
            continue
        for link in links:
            try:
                target = link.resolve(strict=True)
            except OSError:
                continue
            if target.is_file():
                paths.add(target)
    return paths


def _iter_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    out: list[Path] = []
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                out.append(path)
        except OSError:
            continue
    return out


def collect_candidates(
    *,
    data_root: Path = Path("/root/data"),
    datasets_root: Path = Path("/root/datasets"),
    repo_root: Path = Path("/root/content_bot_ml"),
    home_root: Path = Path("/root"),
    active_game: str = "pubg",
    now: float | None = None,
    open_paths: set[Path] | None = None,
) -> list[CleanupCandidate]:
    """Return safe deletion candidates ordered by operational value."""
    now = time.time() if now is None else now
    open_paths = _open_paths() if open_paths is None else open_paths
    labeled_ids = _owner_labeled_ids(repo_root, data_root)
    protected_names: set[str] = set()
    exhausted_by_game: dict[str, set[str]] = {}
    for game in GAMES:
        keep, exhausted = _state_keep_names(data_root, game)
        protected_names.update(keep)
        exhausted_by_game[game] = exhausted

    candidates: dict[Path, CleanupCandidate] = {}

    def add(path: Path, priority: int, reason: str, *, min_age_sec: float = 0) -> None:
        try:
            resolved = path.resolve()
            stat = path.stat()
        except OSError:
            return
        if resolved in open_paths or path.name in protected_names:
            return
        video_id = _video_id(path)
        if video_id and video_id in labeled_ids:
            return
        if now - stat.st_mtime < min_age_sec:
            return
        current = candidates.get(resolved)
        item = CleanupCandidate(resolved, priority, stat.st_size, stat.st_mtime, reason)
        if current is None or priority < current.priority:
            candidates[resolved] = item

    # Interrupted downloads and stale render work are always disposable.
    for pattern in ("*.part", "*.f299.mp4", "*.f399.mp4"):
        for path in home_root.rglob(pattern):
            add(path, 0, "stale_partial", min_age_sec=3600)
    for game in GAMES:
        nightly = data_root / game / "youtube_nightly"
        for path in _iter_files(nightly / "hq_work"):
            add(path, 1, f"{game}_stale_hq_work", min_age_sec=6 * 3600)

    # Derived analysis is cheap to regenerate and otherwise grows forever.
    for path in _iter_files(data_root / "panns_audio_cache"):
        add(path, 5, "expired_panns_cache", min_age_sec=8 * 86400)
    for cache_root in [data_root / "vod_analysis_cache", *data_root.glob("*/analysis_cache")]:
        for path in _iter_files(cache_root):
            add(path, 6, "expired_analysis_cache", min_age_sec=14 * 86400)
    for path in _iter_files(data_root / "pubg" / "ranker_features"):
        add(path, 7, "expired_ranker_feature", min_age_sec=30 * 86400)
    for path in _iter_files(data_root / "pubg" / "audio_candidate_cache"):
        add(path, 7, "expired_audio_candidate", min_age_sec=30 * 86400)

    # In PUBG-only operation, old VOD stores for other games are redownloadable.
    for game in GAMES:
        nightly = data_root / game / "youtube_nightly"
        if game != active_game:
            for folder in ("inbox", "parked", "park_timeout", "exhausted", "hold"):
                for path in _iter_files(nightly / folder):
                    add(path, 10, f"inactive_{game}_{folder}", min_age_sec=24 * 3600)

    # Parked/timeout files are already outside the active queue.
    nightly = data_root / active_game / "youtube_nightly"
    for folder in ("parked", "park_timeout", "exhausted"):
        for path in _iter_files(nightly / folder):
            add(path, 20, f"{active_game}_{folder}", min_age_sec=12 * 3600)

    # Exhausted active-game inbox rows are safe; non-exhausted rows remain protected.
    exhausted = exhausted_by_game.get(active_game, set())
    for path in _iter_files(nightly / "inbox"):
        if path.name in exhausted:
            add(path, 30, f"{active_game}_exhausted_inbox")

    # Telegram already owns sent renders; exemplars are separate protected copies.
    for game in GAMES:
        for path in _iter_files(datasets_root / game / "vod_segments"):
            add(path, 40, f"{game}_old_render", min_age_sec=14 * 86400)

    return sorted(candidates.values(), key=lambda row: (row.priority, row.mtime, -row.size))


def cleanup(
    *,
    min_free_gb: float = 15,
    target_free_gb: float = 25,
    max_used_pct: float = 88,
    dry_run: bool = False,
    active_game: str = "pubg",
    data_root: Path = Path("/root/data"),
    datasets_root: Path = Path("/root/datasets"),
    repo_root: Path = Path("/root/content_bot_ml"),
    home_root: Path = Path("/root"),
) -> dict:
    usage = shutil.disk_usage(home_root)
    free_gb = usage.free / 2**30
    used_pct = usage.used * 100.0 / max(usage.total, 1)
    triggered = free_gb < min_free_gb or used_pct >= max_used_pct
    report: dict = {
        "triggered": triggered,
        "dry_run": dry_run,
        "active_game": active_game,
        "before_free_gb": round(free_gb, 2),
        "before_used_pct": round(used_pct, 2),
        "target_free_gb": target_free_gb,
        "deleted_files": 0,
        "reclaimed_gb": 0.0,
        "by_reason": {},
        "errors": [],
    }
    if not triggered:
        return report

    reclaimed = 0
    target_bytes = int(target_free_gb * 2**30)
    for item in collect_candidates(
        data_root=data_root,
        datasets_root=datasets_root,
        repo_root=repo_root,
        home_root=home_root,
        active_game=active_game,
    ):
        if usage.free + reclaimed >= target_bytes:
            break
        try:
            if not dry_run:
                item.path.unlink(missing_ok=True)
            reclaimed += item.size
            report["deleted_files"] += 1
            by_reason = report["by_reason"]
            by_reason[item.reason] = by_reason.get(item.reason, 0) + 1
        except OSError as exc:
            report["errors"].append(f"{item.path}: {exc}")

    after = shutil.disk_usage(home_root) if not dry_run else usage
    report["reclaimed_gb"] = round(reclaimed / 2**30, 2)
    report["after_free_gb"] = round(
        (after.free if not dry_run else usage.free + reclaimed) / 2**30,
        2,
    )
    report["after_used_pct"] = round(after.used * 100.0 / max(after.total, 1), 2)
    report["target_reached"] = report["after_free_gb"] >= target_free_gb
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-free-gb", type=float, default=float(os.environ.get("VOD_CLEANUP_MIN_FREE_GB", "15")))
    parser.add_argument(
        "--target-free-gb",
        type=float,
        default=float(os.environ.get("VOD_CLEANUP_TARGET_FREE_GB", "25")),
    )
    parser.add_argument(
        "--max-used-pct",
        type=float,
        default=float(os.environ.get("VOD_CLEANUP_MAX_USED_PCT", "88")),
    )
    parser.add_argument("--active-game", default=os.environ.get("VOD_CLEANUP_ACTIVE_GAME", "pubg"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = cleanup(
        min_free_gb=max(1, args.min_free_gb),
        target_free_gb=max(args.min_free_gb, args.target_free_gb),
        max_used_pct=min(99, max(1, args.max_used_pct)),
        dry_run=args.dry_run,
        active_game=args.active_game.strip().lower(),
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
