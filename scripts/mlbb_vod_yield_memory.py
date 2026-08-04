#!/usr/bin/env python3
"""MLBB VOD yield memory — learning that drives the live banner/own-kill path.

Stores compact outcomes (not CLIP exemplar videos):
  youtube_id / uploader / hero → banner hits, own-kill accepts/rejects,
  sends, 👍/👎.

Used to:
  - boost discovery candidates that historically yield own kills + likes
  - penalize ally-trap VODs (banners found, own-kill rejects, or disliked sends)
  - prefer inbox VODs / heroes with proven yield
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("mlbb_vod_yield_memory")

_DATA = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))


def memory_path() -> Path:
    raw = (os.environ.get("MLBB_VOD_YIELD_MEMORY") or "").strip()
    return Path(raw) if raw else _DATA / "vod_yield_memory.json"


def enabled() -> bool:
    return os.environ.get("MLBB_VOD_YIELD_MEMORY_ENABLED", "1") == "1"


def _empty() -> dict:
    return {"videos": {}, "uploaders": {}, "heroes": {}, "updated_at": 0}


def load_memory() -> dict:
    path = memory_path()
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("yield memory load failed: %s", exc)
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("videos", {})
    data.setdefault("uploaders", {})
    data.setdefault("heroes", {})
    return data


def save_memory(data: dict) -> None:
    path = memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = time.time()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _bucket(store: dict, key: str) -> dict:
    key = str(key or "").strip()
    if not key:
        return {}
    row = store.get(key)
    if not isinstance(row, dict):
        row = {
            "attempts": 0,
            "banner_hits": 0,
            "own_kill_hits": 0,
            "own_kill_rejects": 0,
            "sent": 0,
            "likes": 0,
            "dislikes": 0,
        }
        store[key] = row
    return row


def _inc(row: dict, field: str, n: int = 1) -> None:
    if not row:
        return
    row[field] = int(row.get(field) or 0) + int(n)


def _touch(row: dict, *, outcome: str = "") -> None:
    if not row:
        return
    row["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if outcome:
        row["last_outcome"] = outcome


def _hero_from_title(title: str) -> str:
    try:
        from mlbb_hero_roles import hero_from_text

        return str(hero_from_text(title) or "")
    except Exception:
        return ""


def record_scan(
    *,
    youtube_id: str,
    uploader: str = "",
    title: str = "",
    banner_hits: int = 0,
    own_kill_hits: int = 0,
    own_kill_rejects: int = 0,
) -> None:
    """After banner discover on a VOD."""
    if not enabled() or not youtube_id:
        return
    data = load_memory()
    hero = _hero_from_title(title)
    vid = _bucket(data["videos"], youtube_id)
    _inc(vid, "attempts")
    _inc(vid, "banner_hits", banner_hits)
    _inc(vid, "own_kill_hits", own_kill_hits)
    _inc(vid, "own_kill_rejects", own_kill_rejects)
    if uploader:
        vid["uploader"] = str(uploader).casefold()
    if hero:
        vid["hero"] = hero
    if title:
        vid["title"] = str(title)[:120]
    outcome = "own_kill" if own_kill_hits > 0 else ("ally_trap" if banner_hits > 0 else "banner_miss")
    _touch(vid, outcome=outcome)

    up = _bucket(data["uploaders"], str(uploader).casefold()) if uploader else {}
    if up:
        _inc(up, "attempts")
        _inc(up, "banner_hits", banner_hits)
        _inc(up, "own_kill_hits", own_kill_hits)
        _inc(up, "own_kill_rejects", own_kill_rejects)
        _touch(up, outcome=outcome)

    hr = _bucket(data["heroes"], hero) if hero else {}
    if hr:
        _inc(hr, "attempts")
        _inc(hr, "banner_hits", banner_hits)
        _inc(hr, "own_kill_hits", own_kill_hits)
        _inc(hr, "own_kill_rejects", own_kill_rejects)
        _touch(hr, outcome=outcome)

    save_memory(data)
    log.info(
        "yield scan id=%s banners=%s own=%s reject=%s outcome=%s",
        youtube_id,
        banner_hits,
        own_kill_hits,
        own_kill_rejects,
        outcome,
    )


def record_send(
    *,
    youtube_id: str,
    uploader: str = "",
    title: str = "",
    sent: int = 1,
) -> None:
    if not enabled() or not youtube_id or sent <= 0:
        return
    data = load_memory()
    hero = _hero_from_title(title)
    vid = _bucket(data["videos"], youtube_id)
    _inc(vid, "sent", sent)
    if uploader:
        vid["uploader"] = str(uploader).casefold()
    if hero:
        vid["hero"] = hero
    _touch(vid, outcome="sent")
    up = _bucket(data["uploaders"], str(uploader).casefold()) if uploader else {}
    if up:
        _inc(up, "sent", sent)
        _touch(up, outcome="sent")
    hr = _bucket(data["heroes"], hero) if hero else {}
    if hr:
        _inc(hr, "sent", sent)
        _touch(hr, outcome="sent")
    save_memory(data)
    log.info("yield send id=%s sent=%s", youtube_id, sent)


def record_feedback(
    *,
    youtube_id: str,
    is_good: bool,
    uploader: str = "",
    title: str = "",
    reason: str = "",
) -> None:
    """Telegram 👍/👎 on a sent montage/segment — primary learning signal."""
    if not enabled() or not youtube_id:
        return
    data = load_memory()
    hero = _hero_from_title(title) or str(
        (data.get("videos") or {}).get(youtube_id, {}).get("hero") or ""
    )
    if not uploader:
        uploader = str((data.get("videos") or {}).get(youtube_id, {}).get("uploader") or "")
    vid = _bucket(data["videos"], youtube_id)
    field = "likes" if is_good else "dislikes"
    _inc(vid, field)
    if reason:
        vid["last_dislike_reason"] = str(reason)[:80]
    if uploader:
        vid["uploader"] = str(uploader).casefold()
    if hero:
        vid["hero"] = hero
    _touch(vid, outcome="liked" if is_good else "disliked")

    up = _bucket(data["uploaders"], str(uploader).casefold()) if uploader else {}
    if up:
        _inc(up, field)
        _touch(up, outcome="liked" if is_good else "disliked")

    hr = _bucket(data["heroes"], hero) if hero else {}
    if hr:
        _inc(hr, field)
        _touch(hr, outcome="liked" if is_good else "disliked")

    save_memory(data)
    log.info(
        "yield feedback id=%s %s reason=%s",
        youtube_id,
        "like" if is_good else "dislike",
        reason or "-",
    )


def row_score(row: dict | None) -> float:
    """Higher = more useful for live own-kill pipeline."""
    if not row:
        return 0.0
    likes = int(row.get("likes") or 0)
    dislikes = int(row.get("dislikes") or 0)
    sent = int(row.get("sent") or 0)
    own = int(row.get("own_kill_hits") or 0)
    rejects = int(row.get("own_kill_rejects") or 0)
    banners = int(row.get("banner_hits") or 0)
    attempts = max(1, int(row.get("attempts") or 0))

    score = 0.0
    score += likes * 4.0
    score += sent * 1.2
    score += own * 1.5
    score -= dislikes * 5.0
    # Ally-trap: banners without own kills.
    if banners > 0 and own == 0:
        score -= 3.0 + min(6.0, rejects * 0.8)
    elif rejects > own * 2 and rejects >= 2:
        score -= 2.5
    # Repeated scans with zero own kills = wasted wall time.
    if attempts >= 2 and own == 0:
        score -= 2.0 * min(attempts, 5)
    # Banner miss: burned discover wall for nothing — hard chill.
    if banners == 0 and own == 0 and attempts >= 1:
        score -= 5.0
        if str(row.get("last_outcome") or "") == "banner_miss":
            score -= 3.0
    # Yield rate bonus.
    score += 2.0 * (own / attempts)
    return score


def candidate_bonus(meta: dict) -> float:
    """Add to YouTube discovery rank for a candidate meta dict."""
    if not enabled():
        return 0.0
    try:
        from youtube_mlbb_vod_prefs import normalize_uploader
    except Exception:
        normalize_uploader = lambda m: str(m.get("uploader") or m.get("channel") or "").casefold()  # noqa: E731

    data = load_memory()
    vid = str(meta.get("id") or meta.get("youtube_id") or "").strip()
    uploader = normalize_uploader(meta)
    title = str(meta.get("title") or "")
    hero = _hero_from_title(title)

    score = 0.0
    if vid and vid in data.get("videos", {}):
        score += row_score(data["videos"][vid]) * 1.2
    if uploader and uploader in data.get("uploaders", {}):
        score += row_score(data["uploaders"][uploader]) * 0.9
    if hero and hero in data.get("heroes", {}):
        score += row_score(data["heroes"][hero]) * 0.7

    # Hard chill: repeatedly disliked uploader.
    up_row = data.get("uploaders", {}).get(uploader) if uploader else None
    if up_row:
        if int(up_row.get("dislikes") or 0) >= 2 and int(up_row.get("likes") or 0) == 0:
            score -= 8.0
        if int(up_row.get("own_kill_rejects") or 0) >= 6 and int(up_row.get("own_kill_hits") or 0) == 0:
            score -= 6.0
    return score


def pick_penalty(youtube_id: str = "", uploader: str = "", title: str = "") -> float:
    """
    Sort key contribution for inbox pick (lower is better in current pick sort).
    Convert score so high yield → lower penalty.
    """
    if not enabled():
        return 0.0
    data = load_memory()
    score = 0.0
    if youtube_id and youtube_id in data.get("videos", {}):
        row = data["videos"][youtube_id]
        # Already burned a full discover with 0 banners — do not re-pick today.
        if (
            int(row.get("banner_hits") or 0) == 0
            and int(row.get("own_kill_hits") or 0) == 0
            and int(row.get("attempts") or 0) >= 1
        ):
            return 5.0
        if str(row.get("last_outcome") or "") == "banner_miss":
            return 5.0
        score += row_score(row)
    up = (uploader or "").casefold()
    if up and up in data.get("uploaders", {}):
        score += row_score(data["uploaders"][up]) * 0.8
    hero = _hero_from_title(title)
    if hero and hero in data.get("heroes", {}):
        score += row_score(data["heroes"][hero]) * 0.5
    # Map score → penalty bucket for sort (negative score = prefer).
    if score >= 4.0:
        return -2.0
    if score >= 1.5:
        return -1.0
    if score <= -4.0:
        return 2.0
    if score <= -1.5:
        return 1.0
    return 0.0


def should_skip_inbox_pick(youtube_id: str = "") -> bool:
    """True when this VOD already proved banner-dead — skip without re-scanning."""
    if not enabled() or not youtube_id:
        return False
    row = (load_memory().get("videos") or {}).get(str(youtube_id))
    if not isinstance(row, dict):
        return False
    attempts = int(row.get("attempts") or 0)
    banners = int(row.get("banner_hits") or 0)
    own = int(row.get("own_kill_hits") or 0)
    # One OCR-blind / ref-blind miss is not proof — allow a retry after code/env fixes.
    miss_after = max(2, int(os.environ.get("MLBB_YIELD_BANNER_MISS_SKIP_AFTER", "2") or "2"))
    if str(row.get("last_outcome") or "") == "banner_miss":
        return attempts >= miss_after and banners == 0 and own == 0
    return attempts >= miss_after and banners == 0 and own == 0


def preferred_heroes(limit: int = 8) -> list[str]:
    """Heroes with best own-kill / like yield for search query rotation."""
    if not enabled():
        return []
    data = load_memory()
    rows: list[tuple[float, str]] = []
    for hid, row in (data.get("heroes") or {}).items():
        if not hid:
            continue
        s = row_score(row)
        if int(row.get("own_kill_hits") or 0) <= 0 and int(row.get("likes") or 0) <= 0:
            continue
        rows.append((s, str(hid)))
    rows.sort(key=lambda x: (-x[0], x[1]))
    return [h for _, h in rows[: max(1, limit)]]


def ally_trap_uploaders(*, min_rejects: int = 4) -> set[str]:
    """Uploaders that keep producing banners without own kills."""
    if not enabled():
        return set()
    out: set[str] = set()
    for up, row in (load_memory().get("uploaders") or {}).items():
        rejects = int(row.get("own_kill_rejects") or 0)
        own = int(row.get("own_kill_hits") or 0)
        dislikes = int(row.get("dislikes") or 0)
        if rejects >= min_rejects and own == 0:
            out.add(str(up).casefold())
        elif dislikes >= 2 and int(row.get("likes") or 0) == 0:
            out.add(str(up).casefold())
    return out


def summary() -> dict:
    data = load_memory()
    return {
        "videos": len(data.get("videos") or {}),
        "uploaders": len(data.get("uploaders") or {}),
        "heroes": len(data.get("heroes") or {}),
        "path": str(memory_path()),
        "top_heroes": preferred_heroes(5),
        "ally_trap_uploaders": sorted(ally_trap_uploaders())[:12],
    }
