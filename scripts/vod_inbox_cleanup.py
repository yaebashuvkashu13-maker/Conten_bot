#!/usr/bin/env python3
"""Delete inbox VOD files that are fully exhausted (no more moments left).

Keeps registry rows + used_youtube_ids so the same YouTube id is not re-downloaded.
Enable/disable: VOD_INBOX_DELETE_EXHAUSTED=1 (default on).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from vod_game_registry import DAILY_GAMES, spec
from vod_state_io import load_json_state, save_json_state

log = logging.getLogger("vod_inbox_cleanup")


def _load_state(game: str) -> dict[str, Any]:
    return load_json_state(
        spec(game).state_path(),
        lambda: {"vods": [], "used_youtube_ids": [], "vod_outcomes": []},
    )


def _save_state(game: str, state: dict[str, Any]) -> None:
    save_json_state(spec(game).state_path(), state)

# reject_reason prefixes / exact values that mean "nothing left to mine".
_SPENT_REASONS = frozenset(
    {
        "all_peaks_blocked",
        "no_combat_peaks",
        "presend_rejected_all_peaks",
    }
)
_SPENT_PREFIXES = (
    "vod_length=",
    "fast_probe",
    "fast_panns",
    "scan_timeout",
)


def delete_exhausted_enabled() -> bool:
    return os.environ.get("VOD_INBOX_DELETE_EXHAUSTED", "1") == "1"


def delete_grace_sec() -> float:
    """Wait after exhaust mark before unlink (avoids racing an in-flight scan)."""
    return max(0.0, float(os.environ.get("VOD_INBOX_DELETE_GRACE_SEC", "120")))


def delete_max_per_run() -> int:
    return max(1, int(os.environ.get("VOD_INBOX_DELETE_MAX_PER_RUN", "40")))


def _reason_spent(reason: str) -> bool:
    r = (reason or "").strip()
    if not r:
        return False
    if r in _SPENT_REASONS:
        return True
    low = r.lower()
    return any(low.startswith(p) for p in _SPENT_PREFIXES)


def is_fully_spent(entry: dict[str, Any] | None) -> bool:
    """True when the VOD is exhausted and no further moments should be sought."""
    if not entry or not entry.get("exhausted"):
        return False
    if entry.get("file_deleted"):
        return False
    if entry.get("keep_file") or entry.get("preserve_file"):
        return False
    if entry.get("last_scan_blocked"):
        return True
    peaks = entry.get("last_pool_peaks")
    if peaks is not None and len(peaks) == 0:
        return True
    if _reason_spent(str(entry.get("reject_reason") or "")):
        return True
    # Default: any exhausted row is eligible (gates already decided it's done).
    return os.environ.get("VOD_INBOX_DELETE_ALL_EXHAUSTED", "1") == "1"


def _entry_mtime_ok(entry: dict[str, Any], path: Path) -> bool:
    grace = delete_grace_sec()
    if grace <= 0:
        return True
    markers = [
        entry.get("exhausted_at"),
        entry.get("last_scan_at"),
        entry.get("updated_at"),
    ]
    ts = 0.0
    for raw in markers:
        try:
            val = float(raw or 0)
        except (TypeError, ValueError):
            val = 0.0
        if val > ts:
            ts = val
    if ts <= 0:
        try:
            ts = path.stat().st_mtime
        except OSError:
            return True
    return (time.time() - ts) >= grace


def _mark_deleted(entry: dict[str, Any], path: Path, *, bytes_freed: int) -> None:
    entry["file_deleted"] = True
    entry["file_deleted_at"] = time.time()
    entry["file_deleted_path"] = str(path)
    entry["file_deleted_bytes"] = int(bytes_freed)
    # Keep id/title/reject_reason; clear live path so pickers skip missing files cleanly.
    entry["path"] = ""


def delete_exhausted_file(entry: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    """Delete one registry entry's inbox file if fully spent. Mutates entry on success."""
    raw = str(entry.get("path") or "").strip()
    if not raw:
        return {"deleted": False, "reason": "no_path"}
    path = Path(raw)
    if not is_fully_spent(entry):
        return {"deleted": False, "reason": "not_spent", "path": str(path)}
    if not path.exists():
        entry["file_deleted"] = True
        entry["path"] = ""
        return {"deleted": False, "reason": "already_missing", "path": str(path)}
    if not _entry_mtime_ok(entry, path):
        return {"deleted": False, "reason": "grace", "path": str(path)}
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {"deleted": False, "reason": f"stat:{exc}", "path": str(path)}
    if dry_run:
        return {"deleted": False, "reason": "dry_run", "path": str(path), "bytes": size}
    try:
        path.unlink()
    except OSError as exc:
        return {"deleted": False, "reason": f"unlink:{exc}", "path": str(path)}
    _mark_deleted(entry, path, bytes_freed=size)
    log.info("deleted exhausted inbox vod=%s bytes=%s reason=%s", path.name, size, entry.get("reject_reason"))
    return {"deleted": True, "path": str(path), "bytes": size, "id": entry.get("id")}


def cleanup_game(game: str, *, dry_run: bool = False, limit: int | None = None) -> dict[str, Any]:
    """Sweep one game's registry and delete spent inbox files."""
    if not delete_exhausted_enabled() and not dry_run:
        return {"game": game, "deleted": 0, "skipped": True}
    g = game.strip().lower()
    state = _load_state(g)
    vods = state.get("vods") or []
    inbox = spec(g).inbox()
    cap = limit if limit is not None else delete_max_per_run()
    deleted = 0
    freed = 0
    details: list[dict[str, Any]] = []
    for entry in vods:
        if deleted >= cap:
            break
        if not isinstance(entry, dict):
            continue
        path_s = str(entry.get("path") or "").strip()
        if path_s:
            p = Path(path_s)
            try:
                p.resolve().relative_to(inbox.resolve())
            except (ValueError, OSError):
                # Never delete files outside this game's youtube_nightly/inbox.
                continue
        result = delete_exhausted_file(entry, dry_run=dry_run)
        if result.get("deleted") or result.get("reason") == "already_missing":
            if result.get("deleted"):
                deleted += 1
                freed += int(result.get("bytes") or 0)
            details.append(result)
        elif result.get("reason") == "dry_run":
            details.append(result)
    if not dry_run and (deleted or any(d.get("reason") == "already_missing" for d in details)):
        state["vods"] = vods
        state["inbox_cleanup"] = {
            "last_at": time.time(),
            "last_deleted": deleted,
            "last_freed_bytes": freed,
        }
        _save_state(g, state)
    # Also remove orphan exhausted files present in inbox but missing path in registry
    # (matched by yt_*.mp4 id).
    orphans = _cleanup_orphan_exhausted(g, state, dry_run=dry_run, budget=max(0, cap - deleted))
    deleted += orphans["deleted"]
    freed += orphans["freed"]
    if orphans["deleted"] and not dry_run:
        _save_state(g, state)
    return {
        "game": g,
        "deleted": deleted,
        "freed_bytes": freed,
        "details": details[:20],
        "orphans": orphans["deleted"],
    }


def _cleanup_orphan_exhausted(
    game: str,
    state: dict[str, Any],
    *,
    dry_run: bool,
    budget: int,
) -> dict[str, int]:
    """Delete inbox mp4s whose registry row is exhausted but path field drifted."""
    if budget <= 0:
        return {"deleted": 0, "freed": 0}
    inbox = spec(game).inbox()
    if not inbox.is_dir():
        return {"deleted": 0, "freed": 0}
    by_id = {
        str(row.get("id") or ""): row
        for row in (state.get("vods") or [])
        if isinstance(row, dict) and row.get("id")
    }
    deleted = 0
    freed = 0
    for path in sorted(inbox.glob("yt_*.mp4")):
        if deleted >= budget:
            break
        vid = path.stem.replace("yt_", "")[:11]
        entry = by_id.get(vid)
        if not entry or not entry.get("exhausted") or entry.get("file_deleted"):
            continue
        if not is_fully_spent(entry):
            continue
        if not _entry_mtime_ok(entry, path):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if dry_run:
            deleted += 1
            freed += size
            continue
        try:
            path.unlink()
        except OSError:
            continue
        entry["path"] = str(path)
        _mark_deleted(entry, path, bytes_freed=size)
        deleted += 1
        freed += size
        log.info("deleted orphan exhausted inbox vod=%s bytes=%s", path.name, size)
    return {"deleted": deleted, "freed": freed}


def cleanup_all_games(*, dry_run: bool = False) -> list[dict[str, Any]]:
    return [cleanup_game(g, dry_run=dry_run) for g in DAILY_GAMES]


def cleanup_after_exhaust(
    game: str,
    entry: dict[str, Any] | None,
    *,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call right after marking a VOD exhausted in a feed loop."""
    if not delete_exhausted_enabled() or not entry:
        return {"deleted": False, "reason": "disabled_or_empty"}
    # Stamp exhaust time for grace window.
    entry.setdefault("exhausted_at", time.time())
    if delete_grace_sec() > 0:
        # Defer unlink to the next sweep so grace can elapse.
        return {"deleted": False, "reason": "deferred_grace", "id": entry.get("id")}
    result = delete_exhausted_file(entry)
    if result.get("deleted") or result.get("reason") == "already_missing":
        if state is not None:
            _save_state(game, state)
        else:
            st = _load_state(game)
            vid = str(entry.get("id") or "")
            for row in st.get("vods") or []:
                if str(row.get("id") or "") == vid:
                    row.update(entry)
                    break
            _save_state(game, st)
    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import argparse

    p = argparse.ArgumentParser(description="Delete fully exhausted VOD inbox files")
    p.add_argument("game", nargs="?", default="all", help="mlbb|pubg|standoff|genshin|wot|all")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--no-grace", action="store_true", help="Ignore grace period for this run")
    args = p.parse_args()
    if args.no_grace:
        os.environ["VOD_INBOX_DELETE_GRACE_SEC"] = "0"
    if args.game == "all":
        rows = []
        for g in DAILY_GAMES:
            rows.append(cleanup_game(g, dry_run=args.dry_run, limit=args.limit))
    else:
        rows = [cleanup_game(args.game, dry_run=args.dry_run, limit=args.limit)]
    total_del = sum(int(r.get("deleted") or 0) for r in rows)
    total_free = sum(int(r.get("freed_bytes") or 0) for r in rows)
    for r in rows:
        print(
            f"{r['game']}: deleted={r.get('deleted')} "
            f"freed_gb={int(r.get('freed_bytes') or 0) / (1024**3):.2f} "
            f"orphans={r.get('orphans')}"
        )
    print(f"total deleted={total_del} freed_gb={total_free / (1024**3):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
