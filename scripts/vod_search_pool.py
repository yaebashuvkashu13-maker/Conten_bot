#!/usr/bin/env python3
"""Per-game YouTube VOD search pools with a global request limiter.

Keeps a warm candidate queue per game so feeds spend less wall time on
sequential yt-dlp search when the next download is needed.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from vod_game_registry import DAILY_GAMES, SPECS, spec
from vod_state_io import load_json_state, save_json_state

log = logging.getLogger("vod_search_pool")

_LIMITER_LOCK = threading.Lock()
_LAST_SEARCH_AT = 0.0
_SEARCH_INFLIGHT = 0


def pool_path(game: str) -> Path:
    return spec(game).data_root / "vod_search_pool.json"


def pool_min_size() -> int:
    return max(1, int(os.environ.get("VOD_SEARCH_POOL_MIN", "4")))


def pool_max_size() -> int:
    return max(pool_min_size(), int(os.environ.get("VOD_SEARCH_POOL_MAX", "24")))


def pool_ttl_sec() -> int:
    return max(60, int(os.environ.get("VOD_SEARCH_POOL_TTL_SEC", "3600")))


def search_min_interval_sec() -> float:
    return max(0.0, float(os.environ.get("VOD_SEARCH_MIN_INTERVAL_SEC", "2.5")))


def search_max_concurrent() -> int:
    return max(1, int(os.environ.get("VOD_SEARCH_MAX_CONCURRENT", "2")))


def refresh_workers() -> int:
    return max(1, int(os.environ.get("VOD_SEARCH_POOL_WORKERS", "3")))


def _default_pool() -> dict[str, Any]:
    return {
        "candidates": [],
        "updated_at": 0.0,
        "last_refresh_at": 0.0,
        "last_refresh_ok": 0,
        "discovery_cycle": 0,
        "stats": {},
    }


def load_pool(game: str) -> dict[str, Any]:
    return load_json_state(pool_path(game), _default_pool)


def save_pool(game: str, payload: dict[str, Any]) -> None:
    payload["updated_at"] = time.time()
    save_json_state(pool_path(game), payload)


def acquire_search_slot(timeout: float = 120.0) -> bool:
    """Global YouTube search budget: concurrent cap + min spacing between starts."""
    global _LAST_SEARCH_AT, _SEARCH_INFLIGHT
    deadline = time.time() + max(0.0, timeout)
    interval = search_min_interval_sec()
    max_c = search_max_concurrent()
    while time.time() < deadline:
        with _LIMITER_LOCK:
            now = time.time()
            if _SEARCH_INFLIGHT < max_c and (now - _LAST_SEARCH_AT) >= interval:
                _SEARCH_INFLIGHT += 1
                _LAST_SEARCH_AT = now
                return True
        time.sleep(0.15)
    return False


def release_search_slot() -> None:
    global _SEARCH_INFLIGHT
    with _LIMITER_LOCK:
        _SEARCH_INFLIGHT = max(0, _SEARCH_INFLIGHT - 1)


def _candidate_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or "")[:11]


def _merge_candidates(
    existing: list[dict[str, Any]],
    fresh: list[dict[str, Any]],
    *,
    used: set[str],
    max_size: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in list(fresh) + list(existing):
        vid = _candidate_id(row)
        if len(vid) != 11 or vid in used or vid in seen:
            continue
        seen.add(vid)
        out.append(row)
        if len(out) >= max_size:
            break
    return out


def pool_depth(game: str, used: set[str] | None = None) -> int:
    used = used or set()
    pool = load_pool(game)
    n = 0
    for row in pool.get("candidates") or []:
        vid = _candidate_id(row)
        if len(vid) == 11 and vid not in used:
            n += 1
    return n


def pool_refresh_gap_sec() -> float:
    """Avoid back-to-back YouTube searches when prefetch + feed both run."""
    return max(0.0, float(os.environ.get("VOD_SEARCH_POOL_REFRESH_GAP_SEC", "120")))


def pool_needs_refresh(game: str, used: set[str] | None = None) -> bool:
    used = used or set()
    pool = load_pool(game)
    depth = pool_depth(game, used)
    age = time.time() - float(pool.get("last_refresh_at") or 0)
    if age < pool_refresh_gap_sec():
        return False
    if depth < pool_min_size():
        return True
    return age >= pool_ttl_sec()


def pop_candidate(game: str, used: set[str] | None = None) -> dict[str, Any] | None:
    """Pop best unused candidate from the warm pool (does not hit YouTube)."""
    used = set(used or set())
    pool = load_pool(game)
    kept: list[dict[str, Any]] = []
    pick: dict[str, Any] | None = None
    for row in pool.get("candidates") or []:
        vid = _candidate_id(row)
        if len(vid) != 11 or vid in used:
            continue
        if pick is None:
            pick = row
            used.add(vid)
            continue
        kept.append(row)
    if pick is None:
        return None
    pool["candidates"] = kept
    pool["stats"] = {
        **(pool.get("stats") or {}),
        "last_pop_id": pick.get("id"),
        "last_pop_at": time.time(),
        "depth_after_pop": len(kept),
    }
    save_pool(game, pool)
    return pick


def used_ids_for_game(game: str) -> set[str]:
    """Collect youtube ids already downloaded / registered for a game."""
    g = game.strip().lower()
    used: set[str] = set()
    try:
        from vod_game_registry import load_state

        state = load_state(g)
    except Exception:
        state = {}
    for vid in state.get("used_youtube_ids") or []:
        if isinstance(vid, str) and len(vid) == 11:
            used.add(vid)
    for row in state.get("vods") or []:
        vid = str(row.get("id") or "")
        if len(vid) == 11:
            used.add(vid)
    inbox = SPECS[g].inbox() if g in SPECS else None
    if inbox and inbox.is_dir():
        for path in inbox.glob("yt_*.mp4"):
            vid = path.stem.replace("yt_", "")[:11]
            if len(vid) == 11:
                used.add(vid)
    return used


def discover_candidates_for_game(game: str, env: dict[str, str], used: set[str]) -> list[dict[str, Any]]:
    """Run one discovery cycle for a game (YouTube). Shared by pool refresh + feeds."""
    g = game.strip().lower()
    if g == "mlbb":
        from mlbb_vod_segment_feed import _discover_mlbb_vod_candidates

        return list(_discover_mlbb_vod_candidates(env, used, throttled=True) or [])
    from shooter_vod_segment_feed import _discover_candidates

    batch = list(_discover_candidates(g, env, used) or [])
    if g in ("pubg", "standoff") and batch:
        from youtube_shooter_vod_prefs import rank_discovery_candidates

        batch = rank_discovery_candidates(g, batch)
    return batch


def refresh_game_pool(
    game: str,
    env: dict[str, str] | None = None,
    *,
    used: set[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Fetch one search batch and merge into the per-game pool."""
    g = game.strip().lower()
    env = {**os.environ, **(env or {})}
    used = set(used if used is not None else used_ids_for_game(g))
    pool = load_pool(g)
    if not force and not pool_needs_refresh(g, used):
        return {
            "game": g,
            "refreshed": False,
            "depth": pool_depth(g, used),
            "added": 0,
        }

    if not acquire_search_slot():
        log.warning("search slot timeout game=%s", g)
        return {"game": g, "refreshed": False, "depth": pool_depth(g, used), "added": 0, "error": "slot_timeout"}

    added = 0
    err = ""
    try:
        fresh = discover_candidates_for_game(g, env, used)
        before = {_candidate_id(r) for r in (pool.get("candidates") or [])}
        merged = _merge_candidates(
            list(pool.get("candidates") or []),
            fresh,
            used=used,
            max_size=pool_max_size(),
        )
        added = sum(1 for r in merged if _candidate_id(r) not in before)
        pool["candidates"] = merged
        pool["last_refresh_at"] = time.time()
        pool["last_refresh_ok"] = len(fresh)
        pool["stats"] = {
            **(pool.get("stats") or {}),
            "last_batch": len(fresh),
            "last_added": added,
            "depth": len(merged),
        }
        save_pool(g, pool)
        log.info(
            "search pool refresh game=%s batch=%s added=%s depth=%s",
            g,
            len(fresh),
            added,
            len(merged),
        )
    except Exception as exc:
        err = str(exc)[:200]
        log.exception("search pool refresh failed game=%s", g)
    finally:
        release_search_slot()

    return {
        "game": g,
        "refreshed": not err,
        "depth": pool_depth(g, used),
        "added": added,
        "error": err or None,
    }


def prefetch_pools(
    games: list[str] | tuple[str, ...] | None = None,
    env: dict[str, str] | None = None,
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Refresh pools for several games in parallel (bounded by global limiter)."""
    env = {**os.environ, **(env or {})}
    targets = [g for g in (games or DAILY_GAMES) if g in SPECS]
    if os.environ.get("VOD_SEARCH_POOL_ENABLED", "1") != "1":
        return [{"game": g, "refreshed": False, "skipped": True} for g in targets]

    need = []
    for g in targets:
        used = used_ids_for_game(g)
        if force or pool_needs_refresh(g, used):
            need.append(g)
    if not need:
        return [{"game": g, "refreshed": False, "depth": pool_depth(g)} for g in targets]

    results: list[dict[str, Any]] = []
    workers = min(refresh_workers(), len(need))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vod-search") as ex:
        futs = {ex.submit(refresh_game_pool, g, env, force=force): g for g in need}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:
                g = futs[fut]
                log.exception("prefetch failed game=%s", g)
                results.append({"game": g, "refreshed": False, "error": str(exc)[:200]})
    return results


def pick_or_discover(
    game: str,
    env: dict[str, str],
    used: set[str],
    *,
    discover_fn=None,
) -> dict[str, Any] | None:
    """Prefer warm pool; on miss refresh once and pop again; optional legacy discover_fn fallback."""
    g = game.strip().lower()
    if os.environ.get("VOD_SEARCH_POOL_ENABLED", "1") == "1":
        pick = pop_candidate(g, used)
        if pick:
            return pick
        refresh_game_pool(g, env, used=used, force=True)
        pick = pop_candidate(g, used)
        if pick:
            return pick
    if discover_fn is not None:
        batch = list(discover_fn(g, env, used) or [])
        return batch[0] if batch else None
    batch = discover_candidates_for_game(g, env, used)
    return batch[0] if batch else None
