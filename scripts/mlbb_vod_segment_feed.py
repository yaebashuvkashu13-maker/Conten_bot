#!/usr/bin/env python3
"""
MLBB VOD calibration: send every suitable segment as its own clip (no montage merge).

Owner rates with 👍 Ок / 👎 Не ок buttons — all passing segments, no 3-clip cap.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

MLBB_TITLE_RE = re.compile(r"mobile legends|mlbb|bang bang|мобайл легенд", re.I)
LIVE_TITLE_RE = re.compile(r"🔴|\bLIVE\b|playoffs day|knockout stage|grand finals", re.I)
INBOX = Path("/root/data/mlbb/youtube_nightly/inbox")
HOLD_QUOTA = Path(
    os.environ.get(
        "MLBB_VOD_HOLD_QUOTA",
        str(INBOX.parent / "hold_quota"),
    )
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_vod_intervals import (
    conflicts_any_interval as _conflicts_any_interval,
    interval_gap_sec as _interval_gap_sec,
    segment_duration as _segment_duration_from_row,
    segment_interval as _segment_interval,
)
from mlbb_vod_segment_store import (
    inline_keyboard_markup,
    labeled_ids,
    load_feed_sent,
    load_index,
    mark_feed_sent,
    segment_id,
    segments_root,
    stats,
    upsert_segment,
    vod_youtube_id,
)
from montage_env import strict_peak_env
from preview_gate import validate_clips_before_preview
from strict_montage_direct import discover_strict_candidates, file_sha256
from vod_peak_gap import reserved_sent_only, segment_gap_sec
from vod_scan_state import (
    banner_hits_in_entry,
    max_peak_tries,
    peak_values_from_entry,
    peaks_from_pool,
    pool_peaks_fully_blocked,
    record_vod_scan,
    should_mark_vod_exhausted,
    should_retry_banner_gap,
    should_skip_vod_rescan,
    used_peaks_for_vod,
)
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
PROFILE = "mobile_legends"
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024
SEGMENT_SEC = float(os.environ.get("MLBB_VOD_SEGMENT_SEC", os.environ.get("HIGHLIGHT_WINDOW_SEC", "15")))
STATE_PATH = Path("/root/data/mlbb/vod_segment_state.json")
YTDLP_LOCK_PATH = Path("/tmp/mlbb_vod_ytdlp.lock")
FEED_LOCK_PATH = Path("/tmp/mlbb_vod_segment_feed.lock")
log = logging.getLogger("mlbb_vod_feed")

LONG_VOD_TITLE_RE = re.compile(
    r"\b\d+\s*h(?:our|rs?)?\b|\buncut\b|full\s+stream|live\s+stream|"
    r"час(?:ов)?\s+игр|полный\s+стрим",
    re.I,
)


def _vod_min_sec() -> float:
    return float(os.environ.get("MLBB_VOD_MIN_SEC", "180"))


def _effective_vod_min_sec(meta: dict | None = None) -> float:
    """Allow short savage/maniac montages when title promises kill streak."""
    base = _vod_min_sec()
    if os.environ.get("MLBB_VOD_SAVAGE_SHORT_OK", "1") != "1":
        return base
    if meta is None:
        return base
    try:
        from mlbb_vod_title import title_promises_kill_streak

        title = str(meta.get("title") or "")
        if title_promises_kill_streak(title.lower()):
            return min(base, float(os.environ.get("MLBB_VOD_SAVAGE_SHORT_MIN_SEC", "60")))
    except Exception:
        pass
    return base


def _vod_max_sec() -> float:
    return float(os.environ.get("MLBB_VOD_MAX_SEC", "1200"))


def _vod_target_dur_sec() -> float:
    return float(os.environ.get("MLBB_VOD_TARGET_DUR_SEC", "780"))


def _vod_skip_long_sec() -> float:
    return float(os.environ.get("MLBB_VOD_SKIP_LONG_SEC", str(_vod_max_sec())))


def _vod_min_peak_sec(vod: Path | None = None) -> float:
    """Skip laning/spawn — scale min peak with VOD length."""
    base = float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "420"))
    if vod is None:
        return base
    dur = _ffprobe_duration(vod)
    if dur <= 240:
        return min(base, 45.0)
    if dur <= 480:
        return min(base, 90.0)
    if dur <= 1200:
        return min(base, 60.0)
    return base


def _vod_length_ok(path: Path, dur: float | None = None) -> bool:
    length = dur if dur is not None else _ffprobe_duration(path)
    return _vod_min_sec() <= length <= _vod_max_sec()


def _ffprobe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    try:
        return float((proc.stdout or "0").strip())
    except ValueError:
        return 0.0


def _ffprobe_fps(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,r_frame_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    for token in (proc.stdout or "").split():
        if "/" in token:
            num, den = token.split("/", 1)
            try:
                den_f = float(den)
                if den_f > 0:
                    fps = float(num) / den_f
                    if fps > 1:
                        return fps
            except ValueError:
                continue
    return 30.0


def _seek_preroll_sec(vod: Path, start: float) -> float:
    base = float(os.environ.get("MLBB_SEEK_PREROLL", "8"))
    if _ffprobe_fps(vod) >= 55.0:
        base = max(base, float(os.environ.get("MLBB_SEEK_PREROLL_60FPS", "12")))
    return min(base, max(0.0, start))


def _load_state() -> dict:
    from vod_state_io import load_json_state

    default = {
        "active_vod": "",
        "scanned_vods": [],
        "vods": [],
        "used_youtube_ids": [],
    }
    data = load_json_state(STATE_PATH, default)
    data.setdefault("vods", [])
    data.setdefault("used_youtube_ids", [])
    data.setdefault("scanned_vods", [])
    return data


def _save_state(state: dict) -> None:
    from vod_state_io import save_json_state

    save_json_state(STATE_PATH, state)


def _registry_entry(
    path: Path, *, title: str = "", uploader: str = "", exhausted: bool = False
) -> dict:
    vid = vod_youtube_id(path)
    return {
        "id": vid,
        "path": str(path),
        "title": title or path.name,
        "uploader": uploader,
        "exhausted": exhausted,
        "duration_min": int(_ffprobe_duration(path) // 60),
    }


def _repair_registry_ids(registry: list[dict]) -> bool:
    """Fix legacy truncated ids (yt_tp0aAJ22) so exhausted/skip logic works."""
    changed = False
    for row in registry:
        path = Path(str(row.get("path", "")))
        if not path.exists():
            continue
        correct = vod_youtube_id(path)
        if row.get("id") != correct:
            row["id"] = correct
            changed = True
    return changed


def _repair_registry_paths(registry: list[dict]) -> bool:
    """Fix stale paths (yt_ID.f299.mp4) when the file was remuxed to yt_ID.mp4."""
    changed = False
    for row in registry:
        path = Path(str(row.get("path", "")))
        if path.exists() and path.stat().st_size > 1_000_000:
            continue
        vid = str(row.get("id") or "").strip()
        if not vid and path.name:
            vid = vod_youtube_id(path)
        if not vid:
            continue
        candidates: list[Path] = []
        if INBOX.exists():
            candidates.extend(INBOX.glob(f"yt_{vid}*.mp4"))
            candidates.extend(INBOX.glob(f"*{vid}*.mp4"))
        parent = path.parent
        if parent.exists():
            candidates.extend(parent.glob(f"yt_{vid}*.mp4"))
            candidates.extend(parent.glob(f"*{vid}*.mp4"))
        seen: set[str] = set()
        best: Path | None = None
        best_size = 0
        for cand in candidates:
            key = str(cand.resolve())
            if key in seen:
                continue
            seen.add(key)
            try:
                size = cand.stat().st_size
            except OSError:
                continue
            if size > best_size:
                best = cand
                best_size = size
        if best is not None and best_size > 1_000_000:
            row["path"] = str(best)
            changed = True
            log.info("repaired registry path id=%s -> %s", vid, best.name)
    return changed


def _ensure_registry(env: dict[str, str]) -> list[dict]:
    state = _load_state()
    registry: list[dict] = list(state.get("vods", []))
    if _repair_registry_ids(registry):
        log.info("repaired registry youtube ids")
    if _repair_registry_paths(registry):
        log.info("repaired registry vod paths")
    _prune_dead_registry(registry)
    known = {r.get("id") for r in registry}
    known_paths = {str(r.get("path", "")) for r in registry}
    used = set(state.get("used_youtube_ids", []))

    # Bootstrap owner MLBB VOD + any we downloaded before.
    if INBOX.exists():
        for p in sorted(INBOX.glob("yt_*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
            vid = vod_youtube_id(p)
            if str(p) in known_paths or vid in known:
                continue
            dur = _ffprobe_duration(p)
            if not _vod_length_ok(p, dur):
                continue
            from nightly_youtube_montage import fetch_video_meta

            meta = fetch_video_meta(vid, env) or {"title": p.stem, "id": vid}
            title = str(meta.get("title") or p.stem)
            # Prefer info.json title when yt meta is offline — stem alone fails MLBB_TITLE_RE.
            if title == p.stem or title.startswith("yt_"):
                info = p.with_suffix(".info.json")
                if not info.exists():
                    info = p.parent / f"yt_{vid}.info.json"
                if info.exists():
                    try:
                        title = str(json.loads(info.read_text()).get("title") or title)
                    except Exception:
                        pass
            if LONG_VOD_TITLE_RE.search(title):
                continue
            if vid == "E4Dsp53yvv4" or MLBB_TITLE_RE.search(title) or p.exists():
                # Inbox file on disk is enough — title regex must not block rescans.
                registry.append(_registry_entry(p, title=title))
                known.add(vid)
                known_paths.add(str(p))

    state["vods"] = registry
    state["used_youtube_ids"] = sorted(set(used) | {r.get("id", "") for r in registry if r.get("id")})
    _save_state(state)
    return registry


def _auto_exhaust_oversized(registry: list[dict]) -> int:
    """Drop 1–3 h streams from queue — short matches scan 5–10× faster."""
    limit = _vod_skip_long_sec()
    n = 0
    for row in registry:
        if row.get("exhausted"):
            continue
        path = Path(str(row.get("path", "")))
        if not path.exists():
            continue
        dur = _ffprobe_duration(path)
        if dur > limit:
            row["exhausted"] = True
            n += 1
            log.info("auto-exhaust long vod id=%s dur_min=%.0f", row.get("id", path.name), dur / 60)
    return n


def _vod_richness_rank(row: dict) -> int:
    """
    Prefer VODs that already have unused fight peaks — extract more moments
    from one file instead of cycling ~10 empty downloads.
    Lower is better.
    """
    if row.get("last_scan_blocked"):
        return 2
    peaks = row.get("last_pool_peaks")
    if not isinstance(peaks, list) or not peaks:
        # Unscanned / empty pool: try once, but don't prefer over rich caches.
        return 1 if float(row.get("last_scan_at") or 0) > 0 else 0
    pool_n = len(peaks)
    zero = int(row.get("zero_send_attempts") or 0)
    if pool_n >= 3 and zero < 2:
        return -1  # sticky rich VOD
    if pool_n >= 1 and zero < 3:
        return 0
    return 1


def _pick_available_vod(registry: list[dict]) -> dict | None:
    target = _vod_target_dur_sec()
    exhausted_ids = {
        str(row.get("id") or "")
        for row in registry
        if row.get("exhausted") and row.get("id")
    }
    ranked: list[tuple[int, float, int, int, float, float, float, dict]] = []
    seen_ids: set[str] = set()
    for row in registry:
        vid = str(row.get("id") or "")
        if vid and vid in exhausted_ids:
            continue
        if row.get("exhausted"):
            continue
        if should_skip_vod_rescan(row, game="mlbb"):
            continue
        if vid and vid in seen_ids:
            continue
        path = Path(str(row.get("path", "")))
        if not path.exists():
            continue
        dur = _ffprobe_duration(path)
        if not _vod_length_ok(path, dur):
            continue
        if os.environ.get("MLBB_VOD_SKIP_TANK_SUPPORT", "1") == "1":
            try:
                from mlbb_hero_roles import title_is_tank_support_only

                if title_is_tank_support_only(str(row.get("title") or path.stem)):
                    log.info(
                        "skip tank/support vod id=%s title=%s",
                        vid,
                        str(row.get("title") or "")[:60],
                    )
                    continue
            except Exception:
                pass
        try:
            from mlbb_vod_yield_memory import should_skip_inbox_pick

            if vid and should_skip_inbox_pick(vid):
                log.info("skip yield-dead vod id=%s (banner_miss memory)", vid)
                row["exhausted"] = True
                row["reject_reason"] = row.get("reject_reason") or "yield_banner_miss"
                continue
        except Exception:
            pass
        if vid:
            seen_ids.add(vid)
        scanned = float(row.get("last_scan_at") or 0)
        rich = _vod_richness_rank(row)
        yield_pen = 0.0
        try:
            from mlbb_vod_yield_memory import pick_penalty

            yield_pen = float(
                pick_penalty(
                    youtube_id=vid,
                    uploader=str(row.get("uploader") or ""),
                    title=str(row.get("title") or path.stem),
                )
            )
        except Exception:
            yield_pen = 0.0
        title_rank = 1
        try:
            from mlbb_vod_title import title_kill_count, title_promises_kill_streak

            blob = str(row.get("title") or path.stem).lower()
            kills = title_kill_count(blob)
            if title_promises_kill_streak(blob) or kills >= 12:
                title_rank = -1
            elif kills >= 8:
                title_rank = 0
        except Exception:
            title_rank = 1
        ranked.append(
            (rich, yield_pen, title_rank, 1 if scanned else 0, scanned, abs(dur - target), dur, row)
        )
    if not ranked:
        if _revive_exhausted_inbox_candidates(registry):
            return _pick_available_vod(registry)
        return None
    ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4], item[5]))
    pick = ranked[0][7]
    dur = ranked[0][6]
    log.info(
        "pick vod id=%s dur_min=%.0f target_min=%.0f rich=%s yield_pen=%s title_rank=%s scanned=%s pool=%s",
        pick.get("id", ""),
        dur / 60,
        target / 60,
        ranked[0][0],
        ranked[0][1],
        ranked[0][2],
        bool(ranked[0][3]),
        len(pick.get("last_pool_peaks") or []) if isinstance(pick.get("last_pool_peaks"), list) else 0,
    )
    return pick


def _sync_vod_entry_to_state(state: dict, entry: dict, vod: Path) -> None:
    """Persist per-VOD scan fields (cooldown cache) into state file."""
    vid = str(entry.get("id") or vod_youtube_id(vod))
    path = str(entry.get("path") or vod)
    for row in state.get("vods", []):
        if row.get("id") == vid or row.get("path") == path:
            row.update(entry)
            return
    state.setdefault("vods", []).append(dict(entry))


def _mark_vod_exhausted(vod: Path) -> None:
    vid = vod_youtube_id(vod)
    state = _load_state()
    for row in state.get("vods", []):
        row_path = Path(str(row.get("path", "")))
        if row.get("id") == vid or row_path == vod or row_path.name == vod.name:
            row["exhausted"] = True
            row["id"] = vid
    _save_state(state)


def _hard_finish_mlbb_vod(
    state: dict,
    vod: Path,
    *,
    vid: str,
    reason: str,
    entry: dict | None = None,
    delete_file: bool | None = None,
) -> None:
    """Exhaust + optionally delete so junk guides do not clog the inbox."""
    if entry is None:
        entry = {"id": vid, "path": str(vod), "exhausted": True}
    entry["exhausted"] = True
    entry["reject_reason"] = reason
    entry["id"] = vid
    entry["path"] = str(vod)
    _sync_vod_entry_to_state(state, entry, vod)
    if state.get("active_vod") == vod.name:
        state["active_vod"] = ""
    _save_state(state)
    if delete_file is None:
        delete_file = _mlbb_reliable_mode() and os.environ.get("MLBB_VOD_DELETE_EXHAUSTED", "1") == "1"
    if delete_file:
        try:
            if vod.exists():
                vod.unlink()
                log.info("deleted exhausted vod=%s reason=%s", vod.name, reason)
        except OSError as exc:
            log.warning("delete exhausted failed %s: %s", vod.name, exc)
    if vid and reason in {
        "zero_send_reliable",
        "no_combat_peaks",
        "zero_send",
        "presend_banner_floor",
        "banner_hits_no_send",
    }:
        zs = {str(x) for x in (state.get("zero_send_youtube_ids") or []) if x}
        zs.add(vid)
        state["zero_send_youtube_ids"] = sorted(zs)[-400:]
        _save_state(state)


@contextmanager
def _feed_singleton_lock(blocking: bool = False):
    """Only one VOD feed process — continuous_worker bypasses shell flock."""
    FEED_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = FEED_LOCK_PATH.open("w")
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def _ytdlp_download_lock(blocking: bool = True):
    """One yt-dlp download at a time — same idea as Shorts ingest flock."""
    YTDLP_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = YTDLP_LOCK_PATH.open("w")
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _zero_yield_ttl_sec() -> float:
    return max(3600.0, float(os.environ.get("MLBB_VOD_ZERO_YIELD_TTL_SEC", str(7 * 86400))))


def _zero_yield_uploaders() -> set[str]:
    """Uploaders that recently yielded zero — expired after TTL (not permanent)."""
    state = _load_state()
    now = time.time()
    ttl = _zero_yield_ttl_sec()
    ts_map = state.get("zero_yield_uploaders_ts")
    if isinstance(ts_map, dict) and ts_map:
        alive = {
            str(u).casefold()
            for u, ts in ts_map.items()
            if str(u).strip() and (now - float(ts or 0)) < ttl
        }
        return alive
    # Legacy permanent list — still honor until rewritten with timestamps.
    return {str(u).casefold() for u in state.get("zero_yield_uploaders", []) if str(u).strip()}


def _discovery_starvation_level() -> int:
    """How hard discovery has been failing recently (feed-local + daily cycle)."""
    state = _load_state()
    local = int(state.get("discovery_empty_streak") or 0)
    cycle = 0
    try:
        from daily_game_cycle import load_state as load_cycle

        cycle = int((load_cycle().get("discovery_misses") or {}).get("mlbb") or 0)
    except Exception:
        cycle = 0
    return max(local, cycle)


def _zero_yield_block_active() -> bool:
    """
    Permanent uploader blocklist starves discovery after a few bad channels.

    After N empty discovery rounds, temporarily ignore the blocklist so fresh
    VODs from those channels can be tried again.
    """
    if os.environ.get("MLBB_VOD_BYPASS_ZERO_YIELD", "0") == "1":
        return False
    need = max(2, int(os.environ.get("MLBB_VOD_ZERO_YIELD_BYPASS_AFTER", "3")))
    return _discovery_starvation_level() < need


def _note_discovery_empty(*, kept: int) -> None:
    state = _load_state()
    if kept > 0:
        state["discovery_empty_streak"] = 0
    else:
        state["discovery_empty_streak"] = int(state.get("discovery_empty_streak") or 0) + 1
    _save_state(state)


def _title_promise_revive_ok(title: str) -> bool:
    t = str(title or "")
    if re.search(r"\b(?:savage|maniac|triple\s*kill|double\s*kill|legendary)\b", t, re.I):
        return True
    if re.search(r"саваж|маньяк|тройн|двойн", t, re.I):
        return True
    m = re.search(r"\b(\d{1,2})\s*kills?\b", t, re.I)
    if m and int(m.group(1)) >= 12:
        return True
    return False


def _discovery_effective_used(used: set[str]) -> set[str]:
    """
    When discovery starves, allow re-download of VODs that previously zero-sent.
    Permanent used_youtube_ids (484+) was blocking almost every fresh search hit.
    """
    if os.environ.get("MLBB_VOD_DISCOVERY_REUSE_ZERO_SEND", "1") != "1":
        return used
    need = max(3, int(os.environ.get("MLBB_VOD_DISCOVERY_REUSE_AFTER_MISS", "3")))
    starve = _discovery_starvation_level()
    state = _load_state()
    zero_send = {str(v) for v in (state.get("zero_send_youtube_ids") or []) if v}
    effective = set(used)
    if starve >= need and zero_send:
        freed = effective & zero_send
        if freed:
            effective -= freed
            log.info(
                "discovery reuse: unblocked %s zero-send ids (starve=%s)",
                len(freed),
                starve,
            )
    cap = max(120, int(os.environ.get("MLBB_USED_YOUTUBE_IDS_CAP", "220")))
    if len(effective) > cap:
        # Drop excess IDs that are not in the live inbox registry.
        registry_ids = {
            str(r.get("id") or "")
            for r in (state.get("vods") or [])
            if Path(str(r.get("path") or "")).exists()
        }
        trimmable = [vid for vid in sorted(effective) if vid and vid not in registry_ids]
        drop_n = len(effective) - cap
        for vid in trimmable[:drop_n]:
            effective.discard(vid)
        if drop_n > 0:
            log.info(
                "discovery trim: dropped %s stale used_ids (cap=%s left=%s)",
                min(drop_n, len(trimmable)),
                cap,
                len(effective),
            )
    return effective if effective else used


def _prune_dead_registry(registry: list[dict]) -> int:
    """Drop rows whose mp4 was deleted — registry was 400+ dead entries."""
    kept: list[dict] = []
    dropped = 0
    for row in registry:
        path = Path(str(row.get("path") or ""))
        if not path.exists():
            dropped += 1
            continue
        kept.append(row)
    if dropped:
        log.info("pruned dead registry rows=%s kept=%s", dropped, len(kept))
        registry[:] = kept
    return dropped


def _promote_hold_quota_to_inbox(*, limit: int | None = None) -> int:
    """
    Pull VODs from hold_quota when inbox has nothing to scan.
    Prevents hours of silent discovery-miss loops after a productive VOD is finished.
    """
    if os.environ.get("MLBB_VOD_PROMOTE_HOLD", "1") != "1":
        return 0
    INBOX.mkdir(parents=True, exist_ok=True)
    HOLD_QUOTA.mkdir(parents=True, exist_ok=True)
    min_inbox = max(0, int(os.environ.get("MLBB_VOD_PROMOTE_HOLD_MIN_INBOX", "1")))
    inbox_n = sum(1 for p in INBOX.glob("yt_*.mp4") if p.stat().st_size > 1_000_000)
    if inbox_n >= min_inbox:
        return 0
    if limit is None:
        limit = max(1, int(os.environ.get("MLBB_VOD_PROMOTE_HOLD_LIMIT", "4")))
    moved = 0
    for src in sorted(
        HOLD_QUOTA.glob("yt_*.mp4"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        if moved >= limit:
            break
        if src.stat().st_size < 1_000_000:
            continue
        dest = INBOX / src.name
        if dest.exists():
            continue
        info_src = HOLD_QUOTA / f"{src.stem}.info.json"
        try:
            src.rename(dest)
            if info_src.exists():
                info_src.rename(INBOX / info_src.name)
            moved += 1
            log.info("promoted hold→inbox vod=%s", dest.name)
        except OSError as exc:
            log.warning("promote hold failed %s: %s", src.name, exc)
    return moved


def _unexhaust_inbox_paths(registry: list[dict]) -> int:
    """Clear exhausted flags for files currently sitting in inbox."""
    n = 0
    inbox_names = {p.name for p in INBOX.glob("yt_*.mp4")}
    for row in registry:
        vid = str(row.get("id") or "")
        path = Path(str(row.get("path") or ""))
        name = path.name if path.name in inbox_names else (f"yt_{vid}.mp4" if vid else "")
        if name not in inbox_names:
            continue
        row["path"] = str(INBOX / name)
        if not row.get("exhausted"):
            continue
        reason = str(row.get("reject_reason") or "")
        # Do not revive permanently sent_ok / ally junk — only starve/miss leftovers.
        if reason in {"sent_ok", "ally_trap", "disliked"}:
            continue
        row["exhausted"] = False
        row["reject_reason"] = ""
        row["zero_send_attempts"] = 0
        row["last_scan_at"] = 0
        n += 1
        log.info("unexhaust promoted inbox id=%s reason=%s", row.get("id"), reason or "hold_promote")
    return n


def _inbox_pickable_count(registry: list[dict]) -> int:
    """Non-exhausted / non-yield-dead inbox mp4s the picker can actually take."""
    exhausted = {
        str(row.get("id") or "")
        for row in registry
        if row.get("exhausted") and row.get("id")
    }
    n = 0
    for path in INBOX.glob("yt_*.mp4"):
        try:
            if path.stat().st_size < 1_000_000:
                continue
        except OSError:
            continue
        vid = vod_youtube_id(path)
        if vid and vid in exhausted:
            continue
        if vid:
            try:
                from mlbb_vod_yield_memory import should_skip_inbox_pick

                if should_skip_inbox_pick(vid):
                    continue
            except Exception:
                pass
        n += 1
    return n


def _revive_exhausted_inbox_candidates(
    registry: list[dict],
    *,
    limit: int = 3,
    force: bool = False,
) -> int:
    """
    When discovery is starving, reopen on-disk exhausted VODs that promise
    kill streaks in the title — better than looping empty YouTube search.

    force=True: inbox has only exhausted files (starvation counter may still
    be 0 because AUTO_DOWNLOAD=0 never called discovery).
    """
    if os.environ.get("MLBB_VOD_REVIVE_TITLE", "1") != "1":
        return 0
    if not force:
        if _discovery_starvation_level() < max(
            2, int(os.environ.get("MLBB_VOD_REVIVE_AFTER_MISS", "2"))
        ):
            return 0
    revived = 0
    revive_max = int(os.environ.get("MLBB_VOD_REVIVE_MAX", "1"))
    if force:
        revive_max = max(revive_max, int(os.environ.get("MLBB_VOD_REVIVE_MAX_FORCE", "2")))
    for row in registry:
        if revived >= limit:
            break
        if not row.get("exhausted"):
            continue
        if int(row.get("revive_count") or 0) >= revive_max:
            continue
        path = Path(str(row.get("path") or ""))
        if not path.exists() or path.stat().st_size < 1_000_000:
            continue
        # Only revive files still sitting in inbox (not park_dead).
        if "park_dead" in str(path):
            continue
        title = str(row.get("title") or path.stem)
        if not _title_promise_revive_ok(title):
            continue
        row["exhausted"] = False
        row["revive_count"] = int(row.get("revive_count") or 0) + 1
        row["revive_skip_fast_probe"] = True
        row["reject_reason"] = ""
        row["last_scan_blocked"] = False
        revived += 1
        log.info(
            "revive exhausted inbox id=%s title=%s reason=%s",
            row.get("id") or path.name,
            title[:70],
            "inbox_unpickable" if force else "discovery_starve",
        )
    if revived:
        state = _load_state()
        state["vods"] = registry
        _save_state(state)
    return revived


def _record_zero_yield_uploader(meta: dict | None) -> None:
    if not meta:
        return
    from youtube_mlbb_vod_prefs import normalize_uploader

    uploader = normalize_uploader(meta)
    if not uploader:
        return
    state = _load_state()
    now = time.time()
    ttl = _zero_yield_ttl_sec()
    ts_map = {
        str(u).casefold(): float(ts)
        for u, ts in (state.get("zero_yield_uploaders_ts") or {}).items()
        if str(u).strip() and (now - float(ts or 0)) < ttl
    }
    # Migrate legacy permanent names once, with "now" so they expire under TTL.
    for u in state.get("zero_yield_uploaders", []) or []:
        key = str(u).casefold()
        if key and key not in ts_map:
            ts_map[key] = now
    if uploader in ts_map:
        return
    ts_map[uploader] = now
    # Keep newest 200
    pruned = dict(sorted(ts_map.items(), key=lambda kv: kv[1], reverse=True)[:200])
    state["zero_yield_uploaders_ts"] = pruned
    state["zero_yield_uploaders"] = sorted(pruned.keys())
    _save_state(state)
    log.info("zero-yield uploader blocked: %s (ttl=%.0fh)", uploader, ttl / 3600.0)


def _discover_mlbb_vod_candidates(env: dict[str, str], used: set[str], *, throttled: bool = False) -> list[dict]:
    from nightly_youtube_montage import discover_candidates
    from youtube_mlbb_vod_prefs import (
        DEFAULT_SEARCH_QUERIES,
        normalize_uploader,
        parse_upload_date_ymd,
        passes_mlbb_game_title,
        passes_mlbb_vod_filters,
        passes_upload_freshness,
        pick_vod_search_batch,
        rank_mlbb_vod_candidate,
        vod_discovery_search_cycle,
        vod_max_age_days,
    )

    min_sec = _effective_vod_min_sec()
    max_sec = _vod_max_sec()
    target = _vod_target_dur_sec()
    search_delay = float(os.environ.get("MLBB_VOD_SEARCH_DELAY", "5"))
    search_limit = int(os.environ.get("MLBB_VOD_SEARCH_LIMIT", "25"))
    blocked_uploaders = _zero_yield_uploaders() if _zero_yield_block_active() else set()
    if not blocked_uploaders and _zero_yield_uploaders():
        log.info(
            "discovery: bypass zero_yield blocklist (starvation=%s)",
            _discovery_starvation_level(),
        )
    try:
        from mlbb_vod_yield_memory import ally_trap_uploaders

        traps = ally_trap_uploaders()
        if traps:
            blocked_uploaders = set(blocked_uploaders) | traps
            log.info("discovery: yield-memory ally-trap uploaders=%s", len(traps))
    except Exception:
        pass
    all_queries = [
        q.strip()
        for q in os.environ.get("MLBB_VOD_SEARCH_QUERIES", DEFAULT_SEARCH_QUERIES).split(",")
        if q.strip()
    ]
    batch_size = int(
        os.environ.get(
            "MLBB_VOD_SEARCH_BATCH",
            "3" if throttled else str(min(6, max(3, len(all_queries)))),
        )
    )
    state = _load_state()
    offset = int(state.get("discovery_query_offset", 0))
    queries, next_offset = pick_vod_search_batch(all_queries, offset, batch_size)
    search_cycle = int(state.get("discovery_search_cycle", 0))
    search_params = vod_discovery_search_cycle(search_cycle, env)
    state["discovery_query_offset"] = next_offset
    state["discovery_search_cycle"] = search_cycle + 1
    _save_state(state)
    log.info(
        "discovery batch queries=%s cycle=%s mode=%s",
        len(queries),
        search_cycle,
        search_cycle % 3,
    )

    raw: list[dict] = []
    for idx, query in enumerate(queries):
        if idx > 0:
            time.sleep(search_delay)
        batch = discover_candidates(
            env,
            queries=[query],
            min_sec=min_sec,
            max_sec=max_sec,
            search_limit=search_limit,
            youtube_duration_sp=str(search_params.get("youtube_duration_sp") or ""),
            youtube_search_date=bool(search_params.get("youtube_search_date")),
            youtube_freshness_sp=str(search_params.get("youtube_freshness_sp") or ""),
            max_age_days=int(search_params.get("max_age_days") or vod_max_age_days(env)),
        )
        raw.extend(batch)

    out: list[dict] = []
    seen: set[str] = set()
    skipped: dict[str, int] = {}
    for meta in raw:
        vid = str(meta.get("id") or "")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        title = str(meta.get("title") or "")
        dur = float(meta.get("duration") or 0)
        eff_min = _effective_vod_min_sec(meta)
        if LIVE_TITLE_RE.search(title) or LONG_VOD_TITLE_RE.search(title):
            skipped["live_or_long"] = skipped.get("live_or_long", 0) + 1
            continue
        if vid in used:
            skipped["already_used"] = skipped.get("already_used", 0) + 1
            continue
        if not passes_mlbb_game_title(title):
            skipped["not_mlbb"] = skipped.get("not_mlbb", 0) + 1
            continue
        if dur < eff_min or dur > max_sec:
            skipped["duration"] = skipped.get("duration", 0) + 1
            continue
        uploader = normalize_uploader(meta)
        if uploader and uploader in blocked_uploaders:
            skipped["zero_yield_uploader"] = skipped.get("zero_yield_uploader", 0) + 1
            continue
        if not passes_mlbb_vod_filters(meta):
            skipped["bad_title"] = skipped.get("bad_title", 0) + 1
            log.info("skip bad title id=%s %s", vid, title[:70])
            continue
        if not passes_upload_freshness(meta, max_age_days=int(search_params.get("max_age_days") or vod_max_age_days(env))):
            skipped["stale_upload"] = skipped.get("stale_upload", 0) + 1
            continue
        out.append(meta)
    _note_discovery_empty(kept=len(out))
    if skipped:
        log.info("discovery filtered raw=%s kept=%s skipped=%s", len(raw), len(out), skipped)
    out.sort(
        key=lambda m: (
            -rank_mlbb_vod_candidate(m, target_dur_sec=target),
            -(int(parse_upload_date_ymd(str(m.get("upload_date") or "")) or 0)),
            abs(float(m.get("duration") or 0) - target),
        )
    )
    if out:
        top = out[0]
        log.info(
            "discovery pick id=%s score=%.2f dur_min=%.0f title=%s",
            top.get("id"),
            rank_mlbb_vod_candidate(top, target_dur_sec=target),
            float(top.get("duration") or 0) / 60,
            str(top.get("title", ""))[:70],
        )
    return out


def _download_vod_ytdlp_throttled(url: str, env: dict[str, str], *, video_id: str = "") -> Path:
    from nightly_youtube_montage import parse_youtube_id
    from youtube_download import subprocess_env_no_proxy, ytdlp_cmd, ytdlp_extra_args, youtube_format_for_url

    vid = (video_id or parse_youtube_id(url)).strip()
    if not vid:
        raise ValueError(f"cannot parse youtube id from {url}")

    # VPS often has MLBB_SHORTS_ONLY=1 — that blocks 3–20 min VOD downloads.
    vod_env = {**env, "MLBB_SHORTS_ONLY": "0", "YTDLP_MATCH_FILTER": ""}

    delay = float(os.environ.get("MLBB_VOD_DOWNLOAD_DELAY", "12"))
    if delay > 0:
        time.sleep(delay)
    INBOX.mkdir(parents=True, exist_ok=True)
    template = str(INBOX / "yt_%(id)s.%(ext)s")
    expected = INBOX / f"yt_{vid}.mp4"
    cmd = ytdlp_cmd(vod_env, use_proxy=False) + [
        "--no-playlist",
        "--restrict-filenames",
        "--merge-output-format",
        "mp4",
        "-f",
        youtube_format_for_url(url, vod_env),
        "--sleep-requests",
        vod_env.get("YTDLP_SLEEP_REQUESTS", "1.5"),
        "--sleep-interval",
        vod_env.get("YTDLP_SLEEP_INTERVAL", "4"),
        "--max-sleep-interval",
        vod_env.get("YTDLP_MAX_SLEEP_INTERVAL", "12"),
        *ytdlp_extra_args(vod_env),
        "-o",
        template,
        url,
    ]
    subprocess.run(
        cmd,
        check=True,
        timeout=int(vod_env.get("YOUTUBE_DOWNLOAD_TIMEOUT", "14400")),
        env=subprocess_env_no_proxy(vod_env),
    )
    from video_frame_io import ensure_h264_mp4

    if expected.exists() and expected.stat().st_size > 0:
        return ensure_h264_mp4(expected)
    matches = [p for p in INBOX.glob(f"yt_{vid}*.mp4") if p.stat().st_size > 0]
    if matches:
        return ensure_h264_mp4(max(matches, key=lambda p: p.stat().st_mtime))
    files = [p for p in INBOX.glob("yt_*.mp4") if p.stat().st_size > 0]
    if not files:
        raise RuntimeError(f"yt-dlp produced no mp4 for {url} (id={vid})")
    raise RuntimeError(f"yt-dlp did not create expected file {expected} for {url}")


def _download_new_mlbb_vod(env: dict[str, str], registry: list[dict], *, throttled: bool = True) -> Path | None:
    state = _load_state()
    used = _discovery_effective_used(set(state.get("used_youtube_ids", [])))
    used.update(r.get("id", "") for r in registry if r.get("id"))

    candidates = _discover_mlbb_vod_candidates(env, used, throttled=throttled)
    if not candidates:
        return None
    pick = candidates[0]

    with _ytdlp_download_lock(blocking=True) as acquired:
        if not acquired:
            log.warning("yt-dlp lock busy — skip download")
            return None
        path = _download_vod_ytdlp_throttled(
            str(pick.get("url") or f"https://www.youtube.com/watch?v={pick['id']}"),
            env,
            video_id=str(pick.get("id") or ""),
        )

    entry = _registry_entry(
        path,
        title=str(pick.get("title", ""))[:120],
        uploader=str(pick.get("uploader") or ""),
    )
    registry.append(entry)
    state = _load_state()
    state["vods"] = registry
    state["used_youtube_ids"] = sorted(used | {pick["id"]})
    state["active_vod"] = path.name
    state["pending_download"] = {}
    _save_state(state)
    log.info("downloaded vod=%s title=%s", pick["id"], str(pick.get("title", ""))[:60])
    return path


class VodPipelineDownloader:
    """Background next-VOD download while current VOD is scanned/sent."""

    def __init__(self, env: dict[str, str]):
        self.env = env
        self._thread: threading.Thread | None = None
        self._ready: Path | None = None
        self._error: str | None = None
        self._running = False
        self._lock = threading.Lock()
        self._done = threading.Event()

    def busy(self) -> bool:
        with self._lock:
            return self._running or self._ready is not None

    def start_if_idle(self, registry: list[dict]) -> None:
        with self._lock:
            if self._running or self._ready is not None:
                return
            self._running = True
            self._done.clear()
            reg_snapshot = list(registry)
            self._thread = threading.Thread(
                target=self._worker,
                args=(reg_snapshot,),
                daemon=True,
                name="mlbb-vod-bg-dl",
            )
            self._thread.start()

    def _worker(self, registry: list[dict]) -> None:
        path: Path | None = None
        err = ""
        try:
            state = _load_state()
            pending = state.get("pending_download") or {}
            if pending.get("status") == "downloading":
                log.info("another download already marked in state — skip bg")
            else:
                state["pending_download"] = {
                    "status": "downloading",
                    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                _save_state(state)
                path = _download_new_mlbb_vod(self.env, registry, throttled=True)
        except Exception as exc:
            err = str(exc)
            log.exception("background vod download failed")
        finally:
            state = _load_state()
            if path:
                state["pending_download"] = {"status": "ready", "path": str(path)}
            else:
                state["pending_download"] = {"status": "failed", "error": err[:200]}
            _save_state(state)
            with self._lock:
                self._ready = path
                self._error = err or None
                self._running = False
            self._done.set()

    def pop_ready(self) -> Path | None:
        with self._lock:
            ready = self._ready
            self._ready = None
            return ready

    def wait_ready(self, timeout: float) -> Path | None:
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            ready = self.pop_ready()
            if ready:
                return ready
            with self._lock:
                alive = self._running
            if not alive:
                return self.pop_ready()
            time.sleep(min(5.0, deadline - time.time()))
        return None


def bootstrap_exemplar_segments() -> list[dict]:
    """Send existing owner-marked exemplar clips when auto-scan finds nothing yet."""
    root = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml")) / "data/highlight_exemplars/mobile_legends"
    rows: list[dict] = []
    for label_dir, is_good_hint in (("good", True), ("bad", False)):
        for path in sorted((root / label_dir).glob("E4Dsp53yvv4_*.mp4")):
            stem = path.stem
            parts = stem.split("_")
            if len(parts) < 3:
                continue
            try:
                start = int(parts[1])
            except ValueError:
                continue
            sid = f"E4Dsp53yvv4_{start}"
            rows.append(
                {
                    "segment_id": sid,
                    "path": path,
                    "start": start,
                    "score": 1.0 if is_good_hint else 0.0,
                    "hook_score": 0.0,
                    "bootstrap": True,
                    "prior_label": label_dir,
                }
            )
    return rows


def send_video(
    token: str,
    chat_id: str,
    path: Path,
    caption: str,
    *,
    seg_id: str,
    record_learning: bool = True,
    reply_markup: dict | None = None,
    cycle_game: str | None = None,
) -> bool:
    from mlbb_learning_first import can_send, record_send
    from mlbb_telegram_video import (
        TELEGRAM_DOCUMENT_MAX_BYTES,
        TELEGRAM_MAX_BYTES,
        compress_for_inline_video,
        send_document_file,
        send_hq_files,
        send_video_file,
    )

    game = (cycle_game or os.environ.get("VOD_SEGMENT_GAME") or "mlbb").strip().lower()

    if os.environ.get("DAILY_GAME_CYCLE_ENABLED", "0") == "1":
        from daily_game_cycle import can_send_for_game, record_send as cycle_record_send

        ok_cycle, cycle_reason = can_send_for_game(game, 1)
        if not ok_cycle:
            log.warning("send blocked seg=%s cycle=%s game=%s", seg_id, cycle_reason, game)
            return False

    if record_learning:
        ok_send, reason = can_send(1)
        if not ok_send:
            log.warning("send blocked seg=%s reason=%s", seg_id, reason)
            return False

    markup = reply_markup or inline_keyboard_markup(seg_id)
    deliver = path
    is_temp = False
    if path.stat().st_size > TELEGRAM_MAX_BYTES:
        deliver, is_temp = compress_for_inline_video(path, max_bytes=TELEGRAM_MAX_BYTES)
        if is_temp:
            log.info(
                "telegram compress seg=%s %s -> %s bytes",
                seg_id,
                path.stat().st_size,
                deliver.stat().st_size,
            )

    try:
        sent = False
        send_as_file = os.environ.get("VOD_CALIBRATION_SEND_AS_FILE", "1") == "1"
        if send_as_file and path.stat().st_size <= TELEGRAM_DOCUMENT_MAX_BYTES:
            fname = f"{game.upper()}_{seg_id}.mp4"
            sent = send_hq_files(
                token,
                chat_id,
                path,
                f"{caption}\n📁 файл (без пережатия Telegram)",
                reply_markup=markup,
                filename=fname,
            )
        elif deliver.stat().st_size <= TELEGRAM_MAX_BYTES:
            sent = send_video_file(token, chat_id, deliver, caption, reply_markup=markup)
        elif deliver.stat().st_size <= TELEGRAM_DOCUMENT_MAX_BYTES:
            log.warning(
                "telegram sendVideo too large seg=%s bytes=%s — sendDocument fallback",
                seg_id,
                deliver.stat().st_size,
            )
            sent = send_document_file(
                token,
                chat_id,
                deliver,
                f"{caption}\n(файл — документ, >20MB inline)",
                reply_markup=markup,
            )
        else:
            log.warning("telegram too large seg=%s bytes=%s", seg_id, deliver.stat().st_size)
            return False

        if sent:
            if record_learning:
                record_send(1)
            if os.environ.get("DAILY_GAME_CYCLE_ENABLED", "0") == "1":
                from daily_game_cycle import record_send as cycle_record_send

                cycle_record_send(game, 1)
        return sent
    finally:
        if is_temp:
            deliver.unlink(missing_ok=True)


def send_message(token: str, chat_id: str, text: str) -> None:
    subprocess.run(
        [
            "curl",
            "-sS",
            "--noproxy",
            "*",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"text={text[:3900]}",
            f"https://api.telegram.org/bot{token}/sendMessage",
        ],
        env={k: v for k, v in os.environ.items() if "proxy" not in k.lower()},
        check=False,
        timeout=30,
    )


def _vod_lead_sec() -> float:
    try:
        from mlbb_fight_segment import banner_lead_sec

        return float(banner_lead_sec(1))
    except Exception:
        return float(os.environ.get("MLBB_KILL_BANNER_LEAD_SEC", os.environ.get("MLBB_VOD_LEAD_SEC", "8")))


def _banner_row_meta(row: dict) -> tuple[int, str, str]:
    """Parse (tier, source, label) from a clip/send row — single source of truth."""
    tier_i = 0
    try:
        tier_raw = row.get("kill_banner_tier")
        if tier_raw is None and isinstance(row.get("kill_banner"), dict):
            tier_raw = (row.get("kill_banner") or {}).get("tier")
        tier_i = int(tier_raw) if tier_raw is not None else 0
    except (TypeError, ValueError):
        tier_i = 0
    src = str(
        row.get("banner_source")
        or row.get("kill_banner_source")
        or (row.get("clip") or {}).get("banner_source")
        or ""
    ).lower()
    label = str(row.get("kill_banner") or "").lower()
    if isinstance(row.get("kill_banner"), dict):
        label = str((row.get("kill_banner") or {}).get("label") or label).lower()
        if not src:
            src = str((row.get("kill_banner") or {}).get("source") or "").lower()
    return tier_i, src, label


def _reject_ocr_single_send(src: str, label: str, tier_i: int, *, hud_own: bool) -> str | None:
    """Shared OCR-single send gate. Returns reject reason or None."""
    if os.environ.get("MLBB_BANNER_REJECT_OCR_SINGLE", "1") != "1":
        return None
    if tier_i > 1:
        return None
    if os.environ.get("MLBB_ALLOW_OCR_SINGLE_SEND", "0") == "1":
        return None
    if hud_own:
        return None
    ocr_like = (
        src.startswith("ocr")
        or src.startswith("color")
        or not src
        or label in {"single", "single_weak"}
    )
    if not ocr_like:
        return None
    return f"ocr_single_reject:{label or src or 'tier1'}"


_SIG_CACHE: dict[str, tuple[int, int, str]] = {}


def _vod_signature(vod: Path) -> str:
    """SHA256 of VOD with size+mtime cache — full hash of multi‑GB files is slow."""
    try:
        st = vod.stat()
        key = str(vod.resolve())
        cached = _SIG_CACHE.get(key)
        if cached and cached[0] == st.st_size and cached[1] == int(st.st_mtime):
            return cached[2]
        sig = file_sha256(vod)
        _SIG_CACHE[key] = (st.st_size, int(st.st_mtime), sig)
        return sig
    except OSError:
        return file_sha256(vod)


def _apply_lead_start(start: float) -> float:
    return max(0.0, start - _vod_lead_sec())


def _normalize_clip(clip: dict, vod: Path) -> dict:
    peak = float(clip.get("start", 0))
    if os.environ.get("MLBB_VOD_VARIABLE_LENGTH", "1") == "1":
        from mlbb_fight_segment import _analysis_for
        from mlbb_kill_banner import resolve_fight_bounds

        analysis = _analysis_for(vod)
        file_dur = float(analysis.get("duration") or 0.0)
        if file_dur <= 0:
            file_dur = _ffprobe_duration(vod)
        resolved = resolve_fight_bounds(vod, peak, file_dur, clip_meta=clip)
        if resolved is None:
            from mlbb_kill_banner import _motion_anchor_ok

            if _motion_anchor_ok():
                fight_start, fight_end, fight_dur = detect_fight_bounds(vod, peak)
                min_fight = float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7"))
                if fight_dur >= min_fight:
                    from mlbb_teamfight_detector import (
                        combined_teamfight_score,
                        passes_teamfight_threshold,
                    )

                    tf = combined_teamfight_score(analysis, peak, video_path=vod)
                    if not passes_teamfight_threshold(tf):
                        return {
                            **clip,
                            "start": peak,
                            "peak_start": peak,
                            "input_duration": 0.0,
                            "output_duration": 0.0,
                            "banner_reject": f"motion_teamfight_low={tf:.3f}",
                            "source_path": str(vod),
                            "source_index": 0,
                            "speed": 1.0,
                        }
                    resolved = (
                        fight_start,
                        fight_end,
                        fight_dur,
                        {"anchor": "motion", "banner_sec": peak, "teamfight_score": tf},
                    )
        if resolved is None:
            return {
                **clip,
                "start": peak,
                "peak_start": peak,
                "input_duration": 0.0,
                "output_duration": 0.0,
                "banner_reject": "no_streak_banner",
                "source_path": str(vod),
                "source_index": 0,
                "speed": 1.0,
            }
        start, end, dur, meta = resolved
        banner_sec = float(meta.get("banner_sec", peak))
        if str(meta.get("anchor") or "") == "motion":
            from mlbb_teamfight_detector import (
                combined_teamfight_score,
                passes_teamfight_threshold,
            )

            tf = float(meta.get("teamfight_score") or 0.0)
            if tf <= 0:
                tf = combined_teamfight_score(analysis, peak, video_path=vod)
            if not passes_teamfight_threshold(tf):
                return {
                    **clip,
                    "start": peak,
                    "peak_start": peak,
                    "input_duration": 0.0,
                    "output_duration": 0.0,
                    "banner_reject": f"motion_teamfight_low={tf:.3f}",
                    "source_path": str(vod),
                    "source_index": 0,
                    "speed": 1.0,
                }
            meta = {**meta, "teamfight_score": tf}
        try:
            from mlbb_vod_montage import trim_idle_run_end

            # Motion-anchor peaks are not kill moments — run-trim often chops mid-fight.
            if str(meta.get("anchor") or "") != "motion" or os.environ.get(
                "MLBB_VOD_TRIM_MOTION_ANCHOR", "0"
            ) == "1":
                end = trim_idle_run_end(vod, start, end, banner_sec=banner_sec)
                # Hard cut ~3s after kill banner (banner = last kill of this moment).
                post = float(os.environ.get("MLBB_BANNER_POST_SEC", "3"))
                if os.environ.get("MLBB_BANNER_HARD_POST_CUT", "1") == "1" and banner_sec is not None:
                    try:
                        from mlbb_fight_segment import banner_post_sec, banner_lead_sec

                        post = banner_post_sec()
                        tier_i = int(meta.get("banner_tier") or meta.get("kill_banner_tier") or 0)
                        lead_cap = banner_lead_sec(tier_i or 1)
                        start = max(start, float(banner_sec) - lead_cap)
                    except Exception:
                        pass
                    end = min(end, float(banner_sec) + post)
                    dur = max(4.0, end - start)
                    hard_dur = max(4.0, float(banner_sec) + post - start)
                    if hard_dur >= 4.0:
                        dur = min(dur, hard_dur)
                        end = start + dur
                else:
                    dur = max(float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7")), end - start)
        except Exception:
            pass
        return {
            **clip,
            "start": start,
            "peak_start": banner_sec,
            "fight_end": end,
            "source_path": str(vod),
            "source_index": 0,
            "input_duration": dur,
            "output_duration": dur,
            "speed": 1.0,
            **meta,
        }

    from smart_video_editor import profile_action_clip_bounds

    _, clip_hi = profile_action_clip_bounds(PROFILE)
    dur = float(os.environ.get("MLBB_VOD_SEGMENT_SEC", str(max(SEGMENT_SEC, clip_hi))))
    start = _apply_lead_start(peak)
    return {
        **clip,
        "start": start,
        "peak_start": peak,
        "source_path": str(vod),
        "source_index": 0,
        "input_duration": dur,
        "output_duration": dur,
        "speed": 1.0,
    }


def _vod_crop_box(vod: Path, start: float, dur: float) -> tuple[int, int, int, int] | None:
    """Full stream frame when MLBB_VOD_NO_CROP=1 — avoids cutting off HUD/minimap."""
    if os.environ.get("MLBB_VOD_NO_CROP", "0") == "1":
        return None
    from smart_video_editor import detect_game_viewport_crop

    crop = detect_game_viewport_crop(vod, start, dur)
    if not crop or len(crop) != 4:
        return None
    x, y, w, h = crop
    if w < 64 or h < 64:
        return None
    return int(x), int(y), int(w), int(h)


def _crop_filter_prefix(vod: Path, start: float, dur: float) -> str:
    crop = _vod_crop_box(vod, start, dur)
    if not crop:
        return ""
    x, y, w, h = crop
    return f"crop={w}:{h}:{x}:{y},"


def _vod_output_vf(crop_prefix: str, *, setpts: bool = False) -> str:
    """Build ffmpeg -vf chain. MLBB_VOD_LANDSCAPE=1 keeps 16:9 (full stream), not vertical Shorts."""
    from smart_video_editor import OUTPUT_FPS, TARGET_HEIGHT, TARGET_WIDTH

    pts = ",setpts=PTS-STARTPTS" if setpts else ""
    if os.environ.get("MLBB_VOD_LANDSCAPE", "0") == "1":
        w = int(os.environ.get("MLBB_VOD_OUT_WIDTH", "1280"))
        h = int(os.environ.get("MLBB_VOD_OUT_HEIGHT", "720"))
        return (
            f"{crop_prefix}"
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,"
            f"fps={OUTPUT_FPS}{pts},format=yuv420p"
        )
    return (
        f"{crop_prefix}"
        f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={OUTPUT_FPS}{pts},format=yuv420p"
    )


def _needs_chunk_render(vod: Path) -> bool:
    if os.environ.get("MLBB_VOD_CHUNK_RENDER", "1") != "1":
        return False
    if _ffprobe_fps(vod) >= 55.0:
        return True
    if vod.stat().st_size > 900_000_000:
        return True
    return _ffprobe_duration(vod) > _vod_max_sec()


def _extract_vod_chunk(vod: Path, rough_seek: float, chunk_dur: float, chunk_path: Path) -> bool:
    """Stage 1: decode short window to CFR chunk — avoids 60fps seek corruption."""
    from smart_video_editor import OUTPUT_FPS

    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-hwaccel",
        "none",
        "-ss",
        f"{rough_seek:.3f}",
        "-i",
        str(vod),
        "-t",
        f"{chunk_dur:.3f}",
        "-vf",
        f"fps={OUTPUT_FPS},format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        os.environ.get("MLBB_CHUNK_PRESET", "ultrafast"),
        "-crf",
        os.environ.get("MLBB_CHUNK_CRF", "20"),
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(chunk_path),
    ]
    try:
        subprocess.run(
            cmd,
            check=True,
            timeout=int(os.environ.get("MLBB_CHUNK_TIMEOUT_SEC", "600")),
            env={k: v for k, v in os.environ.items() if "proxy" not in k.lower()},
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        log.warning("chunk extract failed vod=%s seek=%.1f: %s", vod.name, rough_seek, exc)
        return False
    return chunk_path.exists() and chunk_path.stat().st_size > 100_000


def _vod_audio_filter() -> str:
    """Telegram-friendly audio — heavy game chain can desync after seek."""
    if os.environ.get("MLBB_VOD_SIMPLE_AUDIO", "1") == "1":
        return "aresample=44100,aformat=channel_layouts=stereo"
    from smart_video_editor import game_audio_filter_chain

    return game_audio_filter_chain(1.0)


def _vod_encode_args() -> list[str]:
    from smart_video_editor import OUTPUT_FPS, output_encode_args

    gop = int(float(os.environ.get("MLBB_VOD_GOP_SEC", "1")) * OUTPUT_FPS)
    saved: dict[str, str | None] = {}
    for key, vod_key in (
        ("SMART_OUTPUT_CRF", "MLBB_VOD_ENCODE_CRF"),
        ("SMART_OUTPUT_AUDIO_K", "MLBB_VOD_ENCODE_AUDIO_K"),
        ("SMART_OUTPUT_PRESET", "MLBB_VOD_ENCODE_PRESET"),
    ):
        if os.environ.get(vod_key):
            saved[key] = os.environ.get(key)
            os.environ[key] = str(os.environ[vod_key])
    try:
        args = [
            *output_encode_args(),
            "-g",
            str(gop),
            "-keyint_min",
            str(gop),
            "-sc_threshold",
            "0",
            "-reset_timestamps",
            "1",
        ]
    finally:
        for key, prev in saved.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
    return args


def _render_from_chunk(
    chunk_path: Path,
    *,
    trim_start: float,
    dur: float,
    crop_prefix: str,
    out_path: Path,
    has_audio: bool,
) -> bool:
    """Stage 2: accurate -ss on small CFR chunk — no freeze."""
    from smart_video_editor import (
        TARGET_HEIGHT,
        TARGET_WIDTH,
        OUTPUT_FPS,
        run_command,
    )

    vf = _vod_output_vf(crop_prefix, setpts=True)
    os.environ.setdefault("SMART_OUTPUT_PRESET", "fast")
    cmd = [
        "ffmpeg",
        "-y",
        "-hwaccel",
        "none",
        "-i",
        str(chunk_path),
        "-ss",
        f"{trim_start:.3f}",
        "-t",
        f"{dur:.3f}",
        "-vf",
        vf,
    ]
    if has_audio:
        cmd.extend(["-af", _vod_audio_filter(), "-map", "0:v:0", "-map", "0:a:0?"])
    else:
        cmd.extend(["-an"])
    cmd.extend(_vod_encode_args())
    cmd.append(str(out_path))
    run_command(cmd)
    return out_path.exists() and out_path.stat().st_size > 100_000


def render_single_segment(vod: Path, clip: dict, out_path: Path) -> bool:
    """
    Cut montage-length window without logo.
    Large/60fps VOD: two-stage chunk + accurate cut (no freeze).
    """
    from smart_video_editor import (
        TARGET_HEIGHT,
        TARGET_WIDTH,
        OUTPUT_FPS,
        ffprobe_has_audio,
        run_command,
    )

    clip = _normalize_clip(clip, vod) if clip.get("input_duration") is None else clip
    start = float(clip["start"])
    dur = float(clip["input_duration"])
    pre_roll = _seek_preroll_sec(vod, start)
    rough_seek = max(0.0, start - pre_roll)
    trim_start = start - rough_seek
    crop_prefix = _crop_filter_prefix(vod, start, dur)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    has_audio = ffprobe_has_audio(vod)

    if _needs_chunk_render(vod):
        chunk_dur = trim_start + dur + 3.0
        with tempfile.NamedTemporaryFile(suffix=".chunk.mp4", delete=False) as tmp:
            chunk_path = Path(tmp.name)
        try:
            if not _extract_vod_chunk(vod, rough_seek, chunk_dur, chunk_path):
                log.warning("chunk render fallback vod=%s start=%.1f", vod.name, start)
            else:
                ok = _render_from_chunk(
                    chunk_path,
                    trim_start=trim_start,
                    dur=dur,
                    crop_prefix=crop_prefix,
                    out_path=out_path,
                    has_audio=has_audio,
                )
                if ok:
                    return True
        finally:
            chunk_path.unlink(missing_ok=True)

    vf = (
        f"trim=start={trim_start:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS,"
        + _vod_output_vf(crop_prefix, setpts=False)
    )
    os.environ.setdefault("SMART_OUTPUT_PRESET", "fast")
    cmd = [
        "ffmpeg",
        "-y",
        "-hwaccel",
        "none",
        "-fflags",
        "+genpts+discardcorrupt",
        "-avoid_negative_ts",
        "make_zero",
        "-ss",
        f"{rough_seek:.3f}",
        "-i",
        str(vod),
        "-vf",
        vf,
    ]
    if has_audio:
        af = (
            f"atrim=start={trim_start:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS,"
            f"{_vod_audio_filter()}"
        )
        cmd.extend(["-af", af, "-map", "0:v:0", "-map", "0:a:0?"])
    else:
        cmd.extend(["-an"])
    cmd.extend(_vod_encode_args())
    cmd.append(str(out_path))
    run_command(cmd)
    return out_path.exists() and out_path.stat().st_size > 100_000


def _segment_duration(row: dict) -> float:
    return _segment_duration_from_row(row)


def _presend_freeze_min_dur() -> float:
    return float(os.environ.get("MLBB_PRESEND_FREEZE_MIN_DUR", "1.2"))


def _presend_freeze_max_start() -> float:
    return float(os.environ.get("MLBB_PRESEND_FREEZE_MAX_START", "3.0"))


def _presend_min_motion() -> float:
    return float(os.environ.get("MLBB_PRESEND_MIN_MOTION", "0.018"))


def _presend_min_minimap_delta() -> float:
    return float(os.environ.get("MLBB_PRESEND_MIN_MINIMAP_DELTA", "0.012"))


def _parse_freezedetect(stderr: str, *, file_duration: float = 0.0) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    cur: dict[str, float] = {}
    for line in stderr.splitlines():
        if "freeze_start:" in line:
            if cur and "start" in cur:
                if "duration" not in cur:
                    end = cur.get("end", file_duration)
                    cur["duration"] = max(0.0, end - cur["start"])
                rows.append(cur)
            try:
                cur = {"start": float(line.rsplit(":", 1)[-1].strip())}
            except ValueError:
                cur = {}
        elif "freeze_duration:" in line and cur:
            try:
                cur["duration"] = float(line.rsplit(":", 1)[-1].strip())
            except ValueError:
                continue
        elif "freeze_end:" in line and cur:
            try:
                cur["end"] = float(line.rsplit(":", 1)[-1].strip())
            except ValueError:
                continue
            if "duration" not in cur and "start" in cur and "end" in cur:
                cur["duration"] = max(0.0, cur["end"] - cur["start"])
            rows.append(cur)
            cur = {}
    if cur and "start" in cur:
        if "duration" not in cur:
            end = cur.get("end", file_duration)
            cur["duration"] = max(0.0, end - cur["start"]) if end > 0 else file_duration
        rows.append(cur)
    return rows


def _detect_render_freeze(path: Path) -> tuple[bool, str, list[dict[str, float]]]:
    """Reject clips that freeze early in Telegram playback."""
    dur = _ffprobe_duration(path)
    if dur < 1.0:
        return False, "render_too_short", []
    noise = float(os.environ.get("MLBB_PRESEND_FREEZE_NOISE", "0.003"))
    min_dur = _presend_freeze_min_dur()
    max_start = _presend_freeze_max_start()
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path),
            "-vf",
            f"freezedetect=n={noise}:d={min_dur}",
            "-map",
            "0:v:0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("MLBB_PRESEND_FREEZE_TIMEOUT", "120")),
        env={k: v for k, v in os.environ.items() if "proxy" not in k.lower()},
    )
    freezes = _parse_freezedetect(proc.stderr or "", file_duration=dur)
    for fr in freezes:
        start = float(fr.get("start", 0.0))
        fdur = float(fr.get("duration", 0.0))
        if start <= max_start and fdur >= min_dur:
            return (
                False,
                f"freeze@{start:.1f}s:{fdur:.1f}s",
                freezes,
            )
        tail = max(0.0, dur - start)
        if fdur >= max(min_dur, tail * 0.55):
            return (
                False,
                f"freeze_tail@{start:.1f}s:{fdur:.1f}s",
                freezes,
            )
    return True, "freeze_ok", freezes


def _validate_before_send(vod: Path, row: dict, rendered: Path) -> tuple[bool, str, dict]:
    """
    Final gate on rendered mp4 + source window that will be sent.
    Catches Telegram freeze, base spawn, idle lanes — after render.
    """
    from gameplay_gate import (
        score_segment_combat,
        segment_looks_like_draft_or_queue,
        segment_uniform_gameplay_ok,
    )
    from visual_action_check import extract_and_check_segment

    report: dict = {"segment_id": row.get("segment_id", "")}
    cut_start = float(row.get("start", 0))
    peak_start = float(row.get("peak_start", cut_start))
    dur = _segment_duration(row)

    ok, reason, freezes = _detect_render_freeze(rendered)
    report["freezes"] = freezes
    if not ok:
        return False, reason, report

    if os.environ.get("MLBB_VOD_KILL_BANNER", "1") == "1":
        # Always-on quality gates (even when BANNER_PRESEND=0). AJxz OCR-single
        # shipped ally double because full presend block was skipped.
        tier_i, src, label = _banner_row_meta(row)
        banner_sec = float(row.get("banner_sec", peak_start) or peak_start)
        # Cap absurd preroll on the row itself (defense if bounds drifted).
        try:
            from mlbb_fight_segment import banner_lead_sec, banner_post_sec

            lead_cap = banner_lead_sec(tier_i or 1)
            post_cap = banner_post_sec()
            if cut_start < banner_sec - lead_cap - 0.5:
                return (
                    False,
                    f"preroll_too_long={banner_sec - cut_start:.1f}>{lead_cap:.1f}",
                    report,
                )
            clip_end = cut_start + dur
            if clip_end > banner_sec + post_cap + 1.0:
                return (
                    False,
                    f"post_tail_too_long={clip_end - banner_sec:.1f}>{post_cap:.1f}",
                    report,
                )
        except Exception as exc:
            log.debug("preroll/post gate skip: %s", exc)
        if os.environ.get("MLBB_PRESEND_OWN_KILL_RECHECK", "1") == "1":
            try:
                from gameplay_gate import _read_frame_at
                from mlbb_banner_hero_match import validate_own_kill_frame
                from mlbb_kill_banner import (
                    ocr_weak_needs_hud,
                    _live_overlay_text,
                    is_coordination_banner_text,
                    is_enemy_kill_text,
                    classify_banner_text,
                )

                fr = _read_frame_at(vod, banner_sec)
                if fr is not None:
                    live = _live_overlay_text(fr)
                    # Objective announce can peak 1–2s off the stored banner_sec
                    # (2Ww5h0ffYtY_270: Lord Spawned @283, stored double @284).
                    if not (live and is_coordination_banner_text(live)):
                        for off in (-1.0, 1.0, -2.0, 2.0):
                            fr_n = _read_frame_at(vod, max(0.0, banner_sec + off))
                            if fr_n is None:
                                continue
                            near = _live_overlay_text(fr_n)
                            if near and is_coordination_banner_text(near):
                                live = near
                                break
                    if live and is_coordination_banner_text(live):
                        return False, f"live_coordination:{live[:40]}", report
                    if live and is_enemy_kill_text(live):
                        return False, f"live_enemy:{live[:40]}", report
                    # OCR doubles/triples: require LIVE streak text. Discover
                    # tesseract garbage + injected "DOUBLE KILL" shipped jungle
                    # farm as 2Ww5h0ffYtY_270.
                    src_l = str(src or "").lower()
                    if (
                        (src_l.startswith("ocr") or not src_l)
                        and int(tier_i or 0) >= 2
                        and os.environ.get("MLBB_OCR_DOUBLE_REQUIRE_LIVE", "1") == "1"
                    ):
                        live_hit = classify_banner_text(live) if live else None
                        if live_hit is None or int(live_hit.tier or 0) < 2:
                            return False, f"ocr_multi_no_live_streak:{(live or '')[:40]}", report
                    # Use live OCR + stored banner_text only — never invent
                    # DOUBLE KILL from the discover label alone.
                    ocr_blob = " ".join(
                        x
                        for x in (
                            live,
                            str(row.get("banner_text") or row.get("kill_banner_text") or ""),
                        )
                        if x
                    )
                    ok_own, own_reason = validate_own_kill_frame(
                        fr, vod=vod, ocr_text=ocr_blob
                    )
                    report["own_kill_recheck"] = own_reason
                    if not ok_own:
                        return False, f"own_kill_recheck:{own_reason}", report
                    if ocr_weak_needs_hud(src or "ocr", tier_i, str(own_reason)):
                        return False, f"ocr_single_no_hud:{own_reason}", report
            except Exception as exc:
                log.warning("own_kill recheck failed: %s", exc)
                return False, f"own_kill_recheck_error:{exc}", report
        hud_own = str(report.get("own_kill_recheck") or "").startswith("hud_killer_ok")
        ocr_rej = _reject_ocr_single_send(src, label, tier_i, hud_own=hud_own)
        if ocr_rej:
            return False, ocr_rej, report

        # Always enforce send floor — BANNER_PRESEND=0 used to skip this and ship
        # lone singles / false streak labels.
        try:
            from mlbb_kill_banner import send_min_tier

            min_tier = send_min_tier()
            montage_single = os.environ.get("MLBB_PRESEND_MONTAGE_SINGLE", "0") == "1"
            # Lone clip must meet floor; multi-single stitch is handled by montage picker.
            parts = int(row.get("montage_parts") or row.get("n_parts") or 1)
            if tier_i < min_tier and not (montage_single and parts >= 2 and tier_i >= 1):
                return False, f"kill_banner_tier_low={tier_i}:need>={min_tier}", report
        except Exception as exc:
            log.debug("send_min_tier gate skip: %s", exc)

        # Post-banner jog: reject if the tail after the kill is mostly running.
        try:
            from mlbb_vod_montage import clip_run_fraction

            clip_end = cut_start + dur
            post_lo = float(banner_sec) + 0.4
            if clip_end > post_lo + 0.8:
                post_run = clip_run_fraction(
                    vod, post_lo, clip_end, banner_sec=float(banner_sec)
                )
                report["post_run_fraction"] = round(post_run, 3)
                max_post = float(os.environ.get("MLBB_PRESEND_MAX_POST_RUN_FRAC", "0.40"))
                if post_run > max_post:
                    return False, f"post_run_frac={post_run:.2f}>{max_post:.2f}", report
        except Exception as exc:
            log.debug("post_run gate skip: %s", exc)

        presend_banner = os.environ.get("MLBB_VOD_BANNER_PRESEND", "1") == "1"
        montage_single = os.environ.get("MLBB_PRESEND_MONTAGE_SINGLE", "0") == "1"
        if presend_banner:
            from mlbb_kill_banner import (
                verify_banner_on_source,
                verify_rendered_clip,
                _min_tier,
                send_min_tier,
                _may_trust_discover_banner,
            )

            # Never blind-trust OCR "single" discover hits — they FPs on HUD noise
            # (asSYCsoCSPs_959: trusted_discover:single with no real kill).
            trust_discover = _may_trust_discover_banner(row)
            if trust_discover:
                banner_ok, banner_reason = True, f"trusted_discover:{row.get('kill_banner') or row.get('kill_banner_tier')}"
            else:
                # Presend verify uses send floor (double+) unless montage singles stitch.
                verify_need = send_min_tier()
                if montage_single and os.environ.get("MLBB_VOD_MONTAGE_ALLOW_SINGLES", "1") == "1":
                    verify_need = 1
                banner_ok, banner_reason = verify_banner_on_source(
                    vod, banner_sec, min_tier=verify_need
                )
                if not banner_ok:
                    banner_ok, banner_reason = verify_rendered_clip(
                        rendered,
                        banner_sec=banner_sec if row.get("banner_sec") else None,
                        clip_start=cut_start,
                        min_tier=verify_need,
                    )
            report["kill_banner"] = banner_reason
            if not banner_ok:
                return False, banner_reason, report
            if os.environ.get("MLBB_KILL_BANNER_REQUIRED", "1") == "1":
                tier = row.get("kill_banner_tier")
                if tier is None and row.get("kill_banner"):
                    tier = (row.get("kill_banner") or {}).get("tier")
                try:
                    tier_i = int(tier) if tier is not None else 0
                except (TypeError, ValueError):
                    tier_i = 0
                min_tier = send_min_tier()
                if montage_single and os.environ.get("MLBB_VOD_MONTAGE_ALLOW_SINGLES", "1") == "1":
                    min_tier = 1
                # Title "maniac/savage" must not reject a real own-kill double/triple —
                # BR6 shipped tier=3 then died on need>=4 and burned the quota hour.
                if str(banner_reason or "").startswith("trusted_discover"):
                    title_min = 0
                else:
                    title_min = int(os.environ.get("MLBB_VOD_TITLE_MIN_TIER", "0") or 0)
                    title_cap = max(
                        min_tier,
                        int(os.environ.get("MLBB_TITLE_SEND_TIER_CAP", "2") or "2"),
                    )
                    if title_min > title_cap:
                        title_min = title_cap
                if title_min > min_tier:
                    min_tier = title_min
                if tier_i < min_tier:
                    return (
                        False,
                        f"kill_banner_tier_low={tier_i}:need>={min_tier}",
                        report,
                    )
                if label in {"single_weak", "color", "announce"} and tier_i <= 1:
                    return False, f"weak_banner_reject:{label}", report

    crop = _vod_crop_box(vod, cut_start, dur)
    report["crop"] = crop

    for label, t0 in (("cut", cut_start), ("peak", peak_start)):
        motion, mini, skill, _text = score_segment_combat(
            vod, t0, dur, crop_box=crop, sample_frames=6
        )
        report[f"{label}_motion"] = round(motion, 4)
        report[f"{label}_mini_delta"] = round(mini, 4)
        report[f"{label}_skill_delta"] = round(skill, 4)
        if segment_looks_like_draft_or_queue(vod, t0, dur, crop_box=crop):
            return False, f"{label}_spawn_or_draft", report
        if motion < _presend_min_motion() and mini < _presend_min_minimap_delta():
            return False, f"{label}_idle_motion={motion:.4f}", report

    # Pre-banner fight context: reject clips where the kill banner sits on idle/run.
    if (
        os.environ.get("MLBB_VOD_KILL_BANNER", "1") == "1"
        and os.environ.get("MLBB_PRESEND_BANNER_CONTEXT", "1") == "1"
    ):
        banner_sec = float(row.get("banner_sec", peak_start) or peak_start)
        lead = float(os.environ.get("MLBB_KILL_BANNER_LEAD_SEC", os.environ.get("MLBB_VOD_LEAD_SEC", "8")))
        ctx_start = max(0.0, banner_sec - min(lead, 10.0))
        ctx_dur = max(4.0, min(12.0, banner_sec + 3.0 - ctx_start))
        ctx_motion, ctx_mini, ctx_skill, _ = score_segment_combat(
            vod, ctx_start, ctx_dur, crop_box=crop, sample_frames=5
        )
        report["banner_ctx_motion"] = round(ctx_motion, 4)
        report["banner_ctx_mini"] = round(ctx_mini, 4)
        report["banner_ctx_skill"] = round(ctx_skill, 4)
        if segment_looks_like_draft_or_queue(vod, ctx_start, ctx_dur, crop_box=crop):
            return False, "banner_ctx_spawn_or_draft", report
        need_m = _presend_min_motion() * 0.90
        need_mini = _presend_min_minimap_delta() * 0.85
        if ctx_motion < need_m and ctx_mini < need_mini and ctx_skill < need_m:
            return False, f"banner_ctx_idle={ctx_motion:.4f}", report
        # Running-around: high camera motion but no fight HUD (skills/minimap).
        # Idle check above misses this — sprint looks "active" on motion alone.
        if os.environ.get("MLBB_PRESEND_REJECT_RUN", "1") == "1":
            run_m = float(os.environ.get("MLBB_PRESEND_RUN_MOTION_MIN", "0.026"))
            need_skill_run = float(os.environ.get("MLBB_PRESEND_RUN_MIN_SKILL", "0.009"))
            need_mini_run = float(os.environ.get("MLBB_PRESEND_RUN_MIN_MINI", "0.009"))
            if (
                ctx_motion >= run_m
                and ctx_skill < need_skill_run
                and ctx_mini < need_mini_run
            ):
                return (
                    False,
                    f"banner_ctx_run={ctx_motion:.4f}/skill={ctx_skill:.4f}/mini={ctx_mini:.4f}",
                    report,
                )
            # Also require at least some skill OR minimap activity near the banner
            # (real teamfights tick both; lane jog usually ticks neither).
            if os.environ.get("MLBB_PRESEND_REQUIRE_FIGHT_HUD", "1") == "1":
                hud_floor = float(os.environ.get("MLBB_PRESEND_FIGHT_HUD_MIN", "0.008"))
                if ctx_skill < hud_floor and ctx_mini < hud_floor:
                    return (
                        False,
                        f"banner_ctx_no_fight_hud=skill={ctx_skill:.4f}/mini={ctx_mini:.4f}",
                        report,
                    )

    # Analysis-based run density (audio/combat dead while center motion stays high).
    # Trusted own-kill banners already passed HUD gate — run_frac false-rejects
    # real teamfights that include a short rotate (BR6 triple @50s → 0.48>0.42).
    trusted_own = str(
        report.get("kill_banner") or row.get("kill_banner") or ""
    ).startswith("trusted_discover")
    if trusted_own and os.environ.get("MLBB_PRESEND_RUN_TRUST_OWN_KILL", "1") == "1":
        pass
    elif os.environ.get("MLBB_MONTAGE_COMBAT_GATE", "0") == "1" or (
        os.environ.get("MLBB_PRESEND_REJECT_RUN", "1") == "1"
        and os.environ.get("MLBB_VOD_MONTAGE", "0") == "1"
    ):
        try:
            from mlbb_vod_montage import clip_run_fraction

            run_frac = clip_run_fraction(
                vod,
                cut_start,
                cut_start + dur,
                banner_sec=float(row.get("banner_sec", peak_start) or peak_start),
            )
            report["run_fraction"] = round(run_frac, 3)
            max_run = float(os.environ.get("MLBB_PRESEND_MAX_RUN_FRAC", "0.55"))
            if run_frac > max_run:
                return False, f"clip_run_frac={run_frac:.2f}>{max_run:.2f}", report
        except Exception as exc:
            log.debug("run_fraction skip: %s", exc)

    uniform_ok, uniform_reason = segment_uniform_gameplay_ok(
        vod, cut_start, dur, crop_box=crop, profile=PROFILE
    )
    report["uniform_reason"] = uniform_reason
    if not uniform_ok:
        return False, uniform_reason, report

    skip_vis = os.environ.get("MLBB_VOD_PRESEND_SKIP_VISUAL_ON_BANNER", "1") == "1"
    has_banner_meta = bool(
        row.get("kill_banner")
        or row.get("kill_banner_tier")
        or str(row.get("anchor") or "") == "kill_banner"
    )
    banner_ok_txt = str(report.get("kill_banner") or "")
    trust_banner_for_visual = has_banner_meta and (
        banner_ok_txt.startswith("source_banner_ok")
        or banner_ok_txt.startswith("banner_ok")
        or banner_ok_txt.startswith("trusted_discover")
        or ":ref" in banner_ok_txt
        or ":ocr" in banner_ok_txt
    )
    if skip_vis and trust_banner_for_visual:
        report["visual_pass"] = True
        report["visual_skipped"] = "trusted_banner_meta"
        log.info(
            "presend visual skip-upfront %s banner=%s",
            row.get("segment_id"),
            banner_ok_txt,
        )
    else:
        vis = extract_and_check_segment(vod, cut_start, dur, PROFILE, crop_box=crop)
        report["visual_pass"] = vis.get("visual_pass")
        report["visual_fail"] = vis.get("fail_reason", "")
        if not vis.get("visual_pass"):
            # Confirmed kill-banner (OCR/ref) already proves this is a real fight moment —
            # HUD OCR at clip start is unreliable (zoom, death cam, banner flash).
            if skip_vis and has_banner_meta and (
                banner_ok_txt.startswith("source_banner_ok")
                or banner_ok_txt.startswith("banner_ok")
                or ":ref" in banner_ok_txt
                or ":ocr" in banner_ok_txt
            ):
                report["visual_skipped"] = vis.get("fail_reason", "fail")
                log.info(
                    "presend visual soft-skip %s reason=%s banner=%s",
                    row.get("segment_id"),
                    vis.get("fail_reason"),
                    banner_ok_txt,
                )
            else:
                return False, f"visual:{vis.get('fail_reason', 'fail')}", report

    rend_motion, rend_mini, rend_skill, _ = score_segment_combat(
        rendered, 0.0, min(dur, _ffprobe_duration(rendered)), sample_frames=6
    )
    report["render_motion"] = round(rend_motion, 4)
    if rend_motion < _presend_min_motion() * 0.75:
        return False, f"render_idle_motion={rend_motion:.4f}", report

    report["pass_reason"] = row.get("pass_reason") or row.get("gate_reason") or "presend_ok"
    return True, "presend_ok", report


def _format_send_report(row: dict, check: dict) -> str:
    peak = int(row.get("peak_start", row["start"]))
    lines = [
        f"score={row['score']:.4f} hook={row['hook_score']:.3f}",
        f"gate={check.get('pass_reason', row.get('gate_reason', ''))}",
        f"cut@{int(row['start'])}s peak@{peak}s",
        (
            f"motion cut={check.get('cut_motion', 0):.3f} "
            f"peak={check.get('peak_motion', 0):.3f} "
            f"render={check.get('render_motion', 0):.3f}"
        ),
    ]
    if check.get("freezes"):
        lines.append(f"freeze_scan={len(check['freezes'])}")
    return "\n".join(lines)


def _segment_gap_sec() -> float:
    level = 2 if reserved_sent_only() else (
        1 if os.environ.get("MLBB_KILL_BANNER_REQUIRED") == "0" else 0
    )
    gap = segment_gap_sec("mlbb", soften_level=level)
    # Soft retry after all_peaks_blocked with banner hits — allow closer fights.
    if os.environ.get("MLBB_VOD_BANNER_GAP_SOFT", "0") == "1":
        soft = float(os.environ.get("MLBB_VOD_BANNER_PEAK_GAP_SEC", "18"))
        gap = min(gap, soft)
    return gap


def _collect_interval_gap_sec(*, bannered: bool = False) -> float:
    gap = _interval_gap_sec()
    if bannered and os.environ.get("MLBB_VOD_BANNER_GAP_SOFT", "0") == "1":
        soft = float(os.environ.get("MLBB_VOD_BANNER_INTERVAL_GAP_SEC", "4"))
        gap = min(gap, soft)
    return gap


def _parse_start_from_segment_id(sid: str, vid: str, stem: str) -> float | None:
    tail = sid.rsplit("_", 1)[-1]
    try:
        start = float(tail)
    except ValueError:
        return None
    if sid.startswith(f"{vid}_"):
        return start
    if len(vid) >= 8 and vid[:8] in sid:
        return start
    if stem in sid or stem.removeprefix("yt_") in sid:
        return start
    return None


def _used_intervals_for_vod(vod: Path, labeled: set[str], sent: set[str]) -> list[tuple[float, float]]:
    """Reserved [start,end] windows — sent, labeled, and indexed segments for this VOD."""
    vid = vod_youtube_id(vod)
    stem = vod.stem
    fallback_dur = float(os.environ.get("MLBB_FIGHT_HARD_MAX_SEC", "65"))
    intervals: list[tuple[float, float]] = []
    seen_starts: set[int] = set()

    for row in load_index().get("segments", []):
        row_vid = str(row.get("vod_id") or "")
        row_vod = str(row.get("vod") or "")
        if row_vid != vid and vod.name not in row_vod and str(vod) not in row_vod:
            continue
        sid = str(row.get("segment_id") or "")
        if reserved_sent_only() and sid and sid not in sent:
            continue
        start = float(row.get("start", 0))
        dur = float(row.get("duration") or row.get("fight_dur") or 0)
        if dur <= 0:
            path = Path(str(row.get("path", "")))
            if path.exists():
                dur = _ffprobe_duration(path)
        if dur <= 0:
            dur = fallback_dur
        intervals.append((start, start + dur))
        seen_starts.add(int(round(start)))

    id_set = sent if reserved_sent_only() else (labeled | sent)
    for sid in id_set:
        start = _parse_start_from_segment_id(sid, vid, stem)
        if start is None:
            continue
        key = int(round(start))
        if key in seen_starts:
            continue
        seen_starts.add(key)
        intervals.append((start, start + fallback_dur))

    return intervals


def _used_starts_for_vod(vod: Path, labeled: set[str], sent: set[str]) -> list[float]:
    return [start for start, _ in _used_intervals_for_vod(vod, labeled, sent)]


def _dedupe_segments_by_gap(
    rows: list[dict],
    *,
    min_gap: float,
    reserved_intervals: list[tuple[float, float]],
) -> list[dict]:
    """Keep best clip per fight — no time overlap / same kill-banner moment."""
    from mlbb_vod_intervals import fight_anchor_sec, same_fight_anchor

    def _rank_key(r: dict) -> tuple[float, float, float, float]:
        metrics = r.get("highlight_metrics") or {}
        clip_score = float(metrics.get("clip_score") or r.get("clip_score") or 0.0)
        tier = float(r.get("kill_banner_tier") or metrics.get("kill_banner_tier") or 0.0)
        has_banner = 1.0 if (r.get("kill_banner") or tier > 0 or str(r.get("anchor") or "") == "banner") else 0.0
        hook = float(r.get("hook_score") or metrics.get("hook_score") or 0.0)
        # Prefer real kill-banner fights over motion-only soften filler.
        return (has_banner, tier, clip_score, hook)

    gap = _interval_gap_sec()
    ranked = sorted(rows, key=_rank_key, reverse=True)
    taken = list(reserved_intervals)
    chosen: list[dict] = []
    for row in ranked:
        start, end = _segment_interval(row)
        if _conflicts_any_interval(start, end, taken, gap=gap):
            continue
        # Legacy start-only guard for peaks very close together inside same fight blob.
        if any(abs(start - s) < min_gap for s, _ in taken):
            continue
        # Same kill banner / peak → one clip only (soft gap used to ship near-duplicates).
        if any(same_fight_anchor(row, prev) for prev in chosen):
            log.info(
                "dedupe same-fight drop sid=%s anchor=%.0f kept_better",
                row.get("segment_id"),
                fight_anchor_sec(row),
            )
            continue
        taken.append((start, end))
        chosen.append(row)
    # Keep quality order for SEND_ONE (best first), not chronological.
    chosen.sort(key=_rank_key, reverse=True)
    return chosen


def _collect_scan_segments(
    vod: Path,
    sig: str,
    labeled: dict,
    sent: set,
    probe_limit: int,
    *,
    pool: list[dict] | None = None,
    skip_peaks: set[float] | None = None,
    entry: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    from mlbb_vod_adaptive_gate import peak_near_skipped
    from mlbb_vod_montage import montage_enabled, montage_collect_env, montage_max_clips
    from vod_analysis_cache import cache_key_hash
    from vod_scan_state import minimal_pool_from_entry, pool_cache_valid

    with montage_collect_env():
        return _collect_scan_segments_inner(
            vod,
            sig,
            labeled,
            sent,
            probe_limit,
            pool=pool,
            skip_peaks=skip_peaks,
            entry=entry,
            peak_near_skipped=peak_near_skipped,
            montage_on=montage_enabled(),
            montage_cap=montage_max_clips(),
            cache_key_hash=cache_key_hash,
            minimal_pool_from_entry=minimal_pool_from_entry,
            pool_cache_valid=pool_cache_valid,
        )


def _collect_scan_segments_inner(
    vod: Path,
    sig: str,
    labeled: dict,
    sent: set,
    probe_limit: int,
    *,
    pool: list[dict] | None,
    skip_peaks: set[float] | None,
    entry: dict | None,
    peak_near_skipped,
    montage_on: bool,
    montage_cap: int,
    cache_key_hash,
    minimal_pool_from_entry,
    pool_cache_valid,
) -> tuple[list[dict], list[dict]]:
    if pool is None:
        if entry and pool_cache_valid(entry):
            pool = minimal_pool_from_entry(entry)
            log.info(
                "reuse cached peak pool vod=%s peaks=%s age=%.0fs",
                vod.name,
                len(pool),
                time.time() - float(entry.get("last_pool_at") or entry.get("last_scan_at") or 0),
            )
        else:
            from mlbb_fight_segment import clear_analysis_cache

            clear_analysis_cache()
            _prime_banner_discover_env()
            pool = discover_strict_candidates(vod, PROFILE, sig, set())
            if entry is not None:
                entry["last_analysis_cache_key"] = cache_key_hash(vod)
    skip_peaks = skip_peaks or set()
    labeled_set = set(labeled.keys()) if isinstance(labeled, dict) else set(labeled)
    min_gap = _segment_gap_sec()
    reserved_intervals = _used_intervals_for_vod(vod, labeled_set, sent)
    out: list[dict] = []
    min_peak = _vod_min_peak_sec(vod)
    skip_counts = {
        "near_skipped": 0,
        "below_min_peak": 0,
        "banner_reject": 0,
        "short": 0,
        "interval": 0,
        "already_sent": 0,
        "gate": 0,
        "low_clip": 0,
    }
    for clip in pool:
        peak = float(clip.get("start", 0))
        peak_anchor = float(clip.get("peak_start", clip.get("banner_sec", peak)) or peak)
        if peak_near_skipped(peak, skip_peaks) or peak_near_skipped(peak_anchor, skip_peaks):
            skip_counts["near_skipped"] += 1
            continue
        if peak < min_peak:
            skip_counts["below_min_peak"] += 1
            continue
        lead_clip = _normalize_clip(clip, vod)
        if lead_clip.get("banner_reject"):
            skip_counts["banner_reject"] += 1
            log.info(
                "skip peak=%.1f banner_reject=%s",
                peak,
                lead_clip.get("banner_reject"),
            )
            continue
        start = float(lead_clip["start"])
        lead_anchor = float(lead_clip.get("peak_start", lead_clip.get("banner_sec", start)) or start)
        if peak_near_skipped(start, skip_peaks) or peak_near_skipped(lead_anchor, skip_peaks):
            skip_counts["near_skipped"] += 1
            continue
        seg_dur = float(lead_clip.get("input_duration") or 0)
        if seg_dur < float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7")):
            skip_counts["short"] += 1
            log.info("skip peak=%.1f short_banner_clip dur=%.1f", peak, seg_dur)
            continue
        end = start + float(lead_clip.get("input_duration") or _segment_duration({"start": start, "clip": lead_clip}))
        clip_bannered = bool(
            int(lead_clip.get("kill_banner_tier") or 0) > 0
            or lead_clip.get("kill_banner")
            or str(lead_clip.get("anchor") or "") not in {"", "motion"}
        )
        gap = _collect_interval_gap_sec(bannered=clip_bannered)
        if _conflicts_any_interval(start, end, reserved_intervals, gap=gap):
            skip_counts["interval"] += 1
            continue
        sid = segment_id(vod, start)
        if sid in labeled_set or sid in sent:
            skip_counts["already_sent"] += 1
            log.info("skip %s already_sent_or_labeled", sid)
            continue
        hm = clip.get("highlight_metrics") or {}
        skip_revalidate = os.environ.get("MLBB_VOD_SKIP_REVALIDATE", "1") == "1"
        already_scored = bool(hm.get("rule_pass")) or str(hm.get("pass_reason") or "").startswith("mlbb_fight")
        if skip_revalidate and already_scored:
            ok, reason = True, str(hm.get("pass_reason") or "highlight_pass")
            metrics_rows = [hm]
            visual_rows = [{"visual_pass": hm.get("visual_pass", True)}]
        else:
            ok, reason, _, metrics_rows, visual_rows = validate_clips_before_preview(
                vod, PROFILE, [lead_clip]
            )
        if not ok:
            skip_counts["gate"] += 1
            log.info("skip %s gate=%s", sid, reason)
            continue
        metrics = (metrics_rows[0] if metrics_rows else {}) or clip.get("highlight_metrics") or {}
        vis = visual_rows[0] if visual_rows else {}
        clip_score = float(metrics.get("clip_score") or 0.0)
        min_clip = float(os.environ.get("MLBB_VOD_MIN_CLIP_SCORE", "0.05"))
        if clip_score < min_clip and os.environ.get("MLBB_VOD_OWNER_EXEMPLARS", "1") == "1":
            skip_counts["low_clip"] += 1
            log.info("skip %s low_clip_score=%.3f min=%.3f", sid, clip_score, min_clip)
            continue
        out.append(
            {
                "segment_id": sid,
                "clip": lead_clip,
                "start": start,
                "peak_start": float(lead_clip.get("peak_start", peak)),
                "fight_dur": float(lead_clip.get("input_duration", 0)),
                "kill_banner": lead_clip.get("kill_banner"),
                "kill_banner_tier": lead_clip.get("kill_banner_tier"),
                "banner_sec": lead_clip.get("banner_sec"),
                "banner_source": lead_clip.get("banner_source"),
                "anchor": lead_clip.get("anchor"),
                "score": float(clip.get("score") or metrics.get("viral_score") or 0),
                "hook_score": float(metrics.get("hook_score") or (clip.get("highlight_metrics") or {}).get("hook_score") or 0),
                "clip_score": clip_score,
                "highlight_metrics": metrics,
                "visual_pass": vis.get("visual_pass", True),
                "pass_reason": metrics.get("pass_reason") or metrics.get("gate_reason") or "",
                "gate_reason": reason,
            }
        )
        if montage_on:
            if len(out) >= max(montage_cap * 2, 6):
                log.info("montage collect: enough candidates=%s — stop pool walk", len(out))
                break
        elif os.environ.get("MLBB_VOD_SEND_ALL_BANNERS", "1") == "1":
            # Keep walking the pool so every bannered fight is validated.
            max_per = max(1, int(os.environ.get("MLBB_VOD_MAX_PER_VOD", "5")))
            bannered_n = sum(
                1
                for r in out
                if r.get("kill_banner")
                or r.get("kill_banner_tier")
                or str(r.get("anchor") or "") == "kill_banner"
            )
            if bannered_n >= max_per or len(out) >= max_per:
                log.info(
                    "send_all_banners: collected %s bannered / %s total (cap=%s)",
                    bannered_n,
                    len(out),
                    max_per,
                )
                break
        elif os.environ.get("MLBB_VOD_SEND_ONE", "1") == "1":
            log.info("send_one: first validated segment %s — skip validating rest of pool", sid)
            break
    if not out and pool and sum(skip_counts.values()) > 0:
        log.info(
            "collect empty vod=%s pool=%s skips=%s",
            vod.name,
            len(pool),
            {k: v for k, v in skip_counts.items() if v},
        )
        if entry is not None and skip_counts["already_sent"] > 0:
            other = sum(v for k, v in skip_counts.items() if k != "already_sent")
            if skip_counts["already_sent"] >= max(1, other):
                entry["reject_reason"] = "already_sent"
    deduped = _dedupe_segments_by_gap(out, min_gap=min_gap, reserved_intervals=reserved_intervals)
    batch_cap = int(os.environ.get("MLBB_VOD_BATCH_MAX", "0"))
    if montage_on:
        from mlbb_vod_montage import bannered_rows, montage_eligible_rows, montage_max_clips, pick_montage_rows

        picked = pick_montage_rows(deduped)
        if picked:
            log.info(
                "montage pick vod=%s n=%s peaks=%s tiers=%s",
                vod.name,
                len(picked),
                [int(float(r.get("peak_start", r["start"]))) for r in picked],
                [int(r.get("kill_banner_tier") or 0) for r in picked],
            )
            deduped = picked
        else:
            eligible = montage_eligible_rows(deduped)
            if eligible and os.environ.get("MLBB_VOD_MONTAGE_SINGLE_FALLBACK", "1") == "1":
                max_per = max(montage_max_clips(), int(os.environ.get("MLBB_VOD_MAX_PER_VOD", "5") or "5"))
                deduped = eligible[:max_per]
                log.info(
                    "montage eligible fallback vod=%s n=%s peaks=%s tiers=%s",
                    vod.name,
                    len(deduped),
                    [int(float(r.get("peak_start", r["start"]))) for r in deduped],
                    [int(r.get("kill_banner_tier") or 0) for r in deduped],
                )
            else:
                log.info(
                    "montage no eligible fallback vod=%s bannered=%s eligible=%s — skip send",
                    vod.name,
                    len(bannered_rows(deduped)),
                    len(eligible),
                )
                deduped = []
    elif batch_cap > 0:
        deduped = deduped[:batch_cap]
    if len(out) > len(deduped):
        log.info(
            "dedupe vod=%s gap=%.0fs raw=%s unique=%s",
            vod.name,
            min_gap,
            len(out),
            len(deduped),
        )
    return deduped, pool


def _send_segment_batch(
    token: str,
    chat_id: str,
    vod: Path,
    to_send: list[dict],
    sig: str,
) -> tuple[int, int, int, bool]:
    """Render and send segments — montage merge when MLBB_VOD_MONTAGE=1.

    Returns (sent, skipped, send_blocked, permanent_reject).
    """
    from mlbb_learning_first import can_send, daily_send_count, max_daily_sends
    from mlbb_vod_montage import montage_enabled, pick_montage_rows

    montage_on = montage_enabled() and len(to_send) >= 2
    if montage_on:
        picked = pick_montage_rows(to_send)
        if len(picked) >= 2:
            to_send = picked
            log.info("send montage n=%s", len(to_send))
        else:
            montage_on = False
            log.info("montage pick thin — fall back to singles n=%s", len(to_send))

    send_one = os.environ.get("MLBB_VOD_SEND_ONE", "1") == "1"
    send_all_banners = os.environ.get("MLBB_VOD_SEND_ALL_BANNERS", "1") == "1"
    max_per = max(1, int(os.environ.get("MLBB_VOD_MAX_PER_VOD", "5") or "5"))
    if send_all_banners and not montage_on:
        bannered = [
            r
            for r in to_send
            if r.get("kill_banner")
            or r.get("kill_banner_tier")
            or str(r.get("anchor") or "") == "kill_banner"
            or r.get("banner_sec")
        ]
        if bannered:
            # Prefer every verified kill-banner fight; drop motion-only fillers.
            to_send = bannered[:max_per]
            send_one = False
            log.info("send_all_banners: shipping %s bannered clips", len(to_send))
        elif send_one and len(to_send) > 1:
            to_send = to_send[:1]
    elif send_one and not montage_on and len(to_send) > 1:
        to_send = to_send[:1]
    elif (
        not montage_on
        and not send_one
        and not send_all_banners
        and len(to_send) > max_per
    ):
        # Reliable montage-off fallback: still respect per-VOD cap.
        to_send = to_send[:max_per]

    ok_batch, block_reason = can_send(1)
    if not ok_batch:
        log.warning("send batch blocked reason=%s", block_reason)
        send_message(
            token,
            chat_id,
            f"⛔ Отправка кусков приостановлена: {block_reason}\n"
            f"(LEARNING_FIRST gate — проверь MLBB_SEND_ENABLED=1 на VPS)",
        )
        return 0, 0, len(to_send), False

    cap_left = max_daily_sends() - daily_send_count()
    if cap_left <= 0:
        log.info("daily cap reached sent_today=%s cap=%s", daily_send_count(), max_daily_sends())
        return 0, 0, 0, False

    if montage_on:
        n, sk, bl = _send_montage_batch(token, chat_id, vod, to_send, sig)
        return n, sk, bl, False

    if not send_one:
        if len(to_send) > cap_left:
            log.info("daily cap trim batch %s -> %s", len(to_send), cap_left)
            to_send = to_send[:cap_left]
    SEGMENTS_ROOT = segments_root()
    SEGMENTS_ROOT.mkdir(parents=True, exist_ok=True)
    sent_ids: list[str] = []
    skipped: list[str] = []
    send_blocked = 0
    header_sent = False
    for row in to_send:
        sid = row["segment_id"]
        out = SEGMENTS_ROOT / f"seg_{sid}.mp4"
        force = os.environ.get("MLBB_FORCE_RERENDER", "1") == "1"
        if force or not out.exists() or out.stat().st_size < 500_000:
            if not render_single_segment(vod, row["clip"], out):
                skipped.append(f"{sid}:render_fail")
                continue
        presend_ok, presend_reason, presend_report = _validate_before_send(vod, row, out)
        if not presend_ok:
            log.warning("presend REJECT %s reason=%s report=%s", sid, presend_reason, presend_report)
            skipped.append(f"{sid}:{presend_reason}")
            continue
        # Announce only when the first clip is actually ready to upload —
        # otherwise the owner gets a text with no videos for minutes.
        if not send_one and not header_sent:
            seg_sec = int(float(os.environ.get("MLBB_VOD_SEGMENT_SEC", "15")))
            planned = len(to_send) - len(skipped)
            send_message(
                token,
                chat_id,
                f"MLBB VOD — {planned} кусков (~{seg_sec}с)\n"
                f"Стрим: {vod_youtube_id(vod)} ({vod.name})\n"
                f"👍 Ок / 👎 Не ок под каждым\n"
                f"Статистика: 👍{stats()['feedback_yes']} 👎{stats()['feedback_no']}",
            )
            header_sent = True
        seg_dur = _ffprobe_duration(out)
        peak = int(row.get("peak_start", row["start"]))
        report_line = _format_send_report(row, presend_report)
        clip_line = ""
        if row.get("clip_score") is not None:
            clip_line = f"learn={float(row['clip_score']):.3f} | "
        banner_line = ""
        if row.get("kill_banner"):
            banner_line = f"🎯 {str(row['kill_banner']).upper()} @ {peak}s\n"
        caption = (
            f"MLBB кусок #{sid}\n"
            f"{banner_line}"
            f"{vod_youtube_id(vod)} @ {int(row['start'])}s"
            f"{f' (баннер {peak}s)' if banner_line else ''} | {seg_dur:.0f}с\n"
            f"{clip_line}{report_line}\n"
            f"✓ presend\n"
            f"👍 Ок / 👎 Не ок"
        )
        if not send_video(token, chat_id, out, caption, seg_id=sid):
            ok_one, one_reason = can_send(1)
            if not ok_one:
                send_blocked += 1
                log.warning("send blocked seg=%s reason=%s", sid, one_reason)
                continue
            send_message(token, chat_id, f"{caption}\n(файл >20MB — не отправился)")
            continue
        upsert_segment(
            {
                "segment_id": sid,
                "path": str(out),
                "vod": str(vod),
                "vod_id": vod_youtube_id(vod),
                "start": row["start"],
                "duration": seg_dur,
                "fight_dur": float(row.get("fight_dur") or seg_dur),
                "peak_start": row.get("peak_start", row["start"]),
                "score": row["score"],
                "hook_score": row["hook_score"],
                "clip_score": row.get("clip_score"),
                "sig": sig,
                "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        sent_ids.append(sid)
        time.sleep(1.5)
    if skipped:
        log.info("presend skipped=%s", "; ".join(skipped[:12]))
    if not sent_ids and skipped:
        # Reliable mode defaults PRESEND_REJECT_NOTIFY=0 — overnight spam was
        # re-sending the same banner_missing_min_tier=2 warning ~every 8 min.
        if os.environ.get("MLBB_VOD_PRESEND_REJECT_NOTIFY", "0") == "1":
            send_message(
                token,
                chat_id,
                f"⚠️ {vod_youtube_id(vod)}: {len(skipped)} кусков не прошли presend\n"
                + "\n".join(skipped[:6]),
            )
        else:
            log.info(
                "presend reject notify suppressed vod=%s n=%s (set MLBB_VOD_PRESEND_REJECT_NOTIFY=1 to spam)",
                vod.name,
                len(skipped),
            )
    if sent_ids:
        mark_feed_sent(sent_ids)
    permanent = bool(skipped) and all(
        any(
            key in s
            for key in (
                "banner_missing_min_tier",
                "source_banner_missing",
                "banner_reject",
                "no_streak_banner",
            )
        )
        for s in skipped
    )
    return len(sent_ids), len(skipped), send_blocked, permanent


def _send_montage_batch(
    token: str,
    chat_id: str,
    vod: Path,
    to_send: list[dict],
    sig: str,
) -> tuple[int, int, int]:
    """One Telegram video = xfade of 2–4 fight windows from the same VOD."""
    from mlbb_vod_montage import build_montage_id, cleanup_temps, concat_rendered_parts
    from smart_video_editor import ffprobe_duration as _probe_part_dur

    vid = vod_youtube_id(vod)
    mid = build_montage_id(vid, to_send)
    SEGMENTS_ROOT = segments_root()
    SEGMENTS_ROOT.mkdir(parents=True, exist_ok=True)
    out = SEGMENTS_ROOT / f"seg_{mid}.mp4"
    temps: list[Path] = []
    skipped: list[str] = []
    try:
        gated_rows: list[dict] = []
        gated_parts: list[Path] = []
        gated_durs: list[float] = []
        for row in to_send:
            part = Path(tempfile.mkstemp(suffix=".part.mp4")[1])
            temps.append(part)
            if not render_single_segment(vod, row["clip"], part):
                skipped.append(f"{row['segment_id']}:render_fail")
                continue
            prev_montage_single = os.environ.get("MLBB_PRESEND_MONTAGE_SINGLE")
            os.environ["MLBB_PRESEND_MONTAGE_SINGLE"] = "1"
            try:
                ok, reason, _rep = _validate_before_send(vod, row, part)
            finally:
                if prev_montage_single is None:
                    os.environ.pop("MLBB_PRESEND_MONTAGE_SINGLE", None)
                else:
                    os.environ["MLBB_PRESEND_MONTAGE_SINGLE"] = prev_montage_single
            if not ok:
                skipped.append(f"{row['segment_id']}:{reason}")
                continue
            dur = float(row.get("fight_dur") or row["clip"].get("input_duration") or 0)
            if dur < 1:
                dur = float(_probe_part_dur(part) or 0)
            gated_rows.append(row)
            gated_parts.append(part)
            gated_durs.append(dur)
        if len(gated_rows) < 2:
            log.warning("montage aborted — fewer than 2 parts passed gate (%s)", len(gated_rows))
            if gated_rows:
                return _send_single_fallback(token, chat_id, vod, gated_rows[0], sig)
            return 0, len(skipped), 0

        # Keep VOD chronology even if a middle part was dropped by the gate.
        order = sorted(
            range(len(gated_rows)),
            key=lambda i: float(
                gated_rows[i].get("peak_start", gated_rows[i].get("start") or 0) or 0
            ),
        )
        gated_rows = [gated_rows[i] for i in order]
        gated_parts = [gated_parts[i] for i in order]
        gated_durs = [gated_durs[i] for i in order]

        if not concat_rendered_parts(gated_parts, gated_durs, out):
            skipped.append("concat_fail")
            return 0, len(skipped), 0

        banners = []
        for row in gated_rows:
            if row.get("kill_banner"):
                banners.append(
                    f"{str(row['kill_banner']).upper()}@{int(float(row.get('peak_start', row['start'])))}"
                )
        seg_dur = _ffprobe_duration(out)
        caption = (
            f"MLBB склейка #{mid}\n"
            f"🎯 {' · '.join(banners) if banners else f'{len(gated_rows)} моментов'}\n"
            f"{vid} | {len(gated_rows)} куска | {seg_dur:.0f}с\n"
            f"✓ montage (anti-run trim)\n"
            f"👍 Ок / 👎 Не ок"
        )
        if not send_video(token, chat_id, out, caption, seg_id=mid):
            send_message(token, chat_id, f"{caption}\n(файл не отправился)")
            return 0, len(skipped), 1
        upsert_segment(
            {
                "segment_id": mid,
                "path": str(out),
                "vod": str(vod),
                "vod_id": vid,
                "start": gated_rows[0]["start"],
                "duration": seg_dur,
                "fight_dur": seg_dur,
                "peak_start": gated_rows[0].get("peak_start", gated_rows[0]["start"]),
                "score": max(float(r.get("score") or 0) for r in gated_rows),
                "hook_score": max(float(r.get("hook_score") or 0) for r in gated_rows),
                "clip_score": max(float(r.get("clip_score") or 0) for r in gated_rows),
                "montage_parts": [r["segment_id"] for r in gated_rows],
                "sig": sig,
                "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        part_ids = [r["segment_id"] for r in gated_rows] + [mid]
        mark_feed_sent(part_ids)
        try:
            from daily_game_cycle import _today_key
            from montage_dedup import mark_montage_sent

            peaks = [
                float(r.get("peak_start", r.get("start") or 0) or 0) for r in gated_rows
            ]
            mark_montage_sent(
                "mlbb",
                day=_today_key(),
                vod_id=vid,
                peaks=peaks,
                montage_id=mid,
            )
        except Exception as exc:
            log.warning("montage_dedup mark fail: %s", exc)
        log.info("montage sent id=%s parts=%s dur=%.0f", mid, len(gated_rows), seg_dur)
        return 1, len(skipped), 0
    finally:
        cleanup_temps(temps)


def _send_single_fallback(
    token: str,
    chat_id: str,
    vod: Path,
    row: dict,
    sig: str,
) -> tuple[int, int, int]:
    """Send one clip when montage collapsed to a single gated part."""
    old = os.environ.get("MLBB_VOD_MONTAGE")
    os.environ["MLBB_VOD_MONTAGE"] = "0"
    try:
        return _send_segment_batch(token, chat_id, vod, [row], sig)
    finally:
        if old is None:
            os.environ.pop("MLBB_VOD_MONTAGE", None)
        else:
            os.environ["MLBB_VOD_MONTAGE"] = old


def _prime_banner_discover_env() -> None:
    """Pass feed starvation into discover so dense sweep kicks in after misses."""
    starve = _discovery_starvation_level()
    os.environ["MLBB_VOD_DISCOVER_MISS_STREAK"] = str(starve)
    # Fight-first: only force dense after miss streak, never by default.
    if starve >= max(1, int(os.environ.get("MLBB_VOD_DISCOVER_DENSE_AFTER_MISS", "2"))):
        if os.environ.get("MLBB_BANNER_FIGHT_FIRST", "1") == "1":
            os.environ["MLBB_VOD_BANNER_DENSE_SEC"] = "1"
        else:
            os.environ.setdefault("MLBB_VOD_DISCOVER_ALWAYS_DENSE", "1")


def _resolve_next_vod(
    env: dict[str, str],
    registry: list[dict],
    downloader: VodPipelineDownloader,
    *,
    auto_download: bool,
    token: str,
    chat_id: str,
    notify: bool,
) -> tuple[Path | None, dict | None]:
    entry = _pick_available_vod(registry)
    if entry:
        path = Path(str(entry["path"]))
        if path.exists():
            return path, entry
        if _repair_registry_paths(registry):
            path = Path(str(entry["path"]))
            if path.exists():
                return path, entry

    ready = downloader.pop_ready()
    if ready and ready.exists():
        registry[:] = _ensure_registry(env)
        entry = _pick_available_vod(registry)
        if entry:
            return Path(str(entry["path"])), entry

    if not auto_download:
        return None, None

    downloader.start_if_idle(registry)
    ready = downloader.wait_ready(timeout=float(os.environ.get("MLBB_VOD_BG_WAIT_SEC", "120")))
    if ready and ready.exists():
        registry[:] = _ensure_registry(env)
        entry = _pick_available_vod(registry)
        if entry:
            return Path(str(entry["path"])), entry

    if notify:
        send_message(token, chat_id, "📥 Качаю новый MLBB VOD с YouTube (с паузами, без бана)…")
    vod = _download_new_mlbb_vod(env, registry, throttled=True)
    if vod:
        registry[:] = _ensure_registry(env)
        entry = next((r for r in registry if r.get("id") == vod_youtube_id(vod)), None)
        if notify:
            title = str((entry or {}).get("title") or vod.name)
            send_message(
                token,
                chat_id,
                f"✅ Скачал: {title[:80]}\n"
                f"Сканирую куски (~{int(_ffprobe_duration(vod) // 60)} мин стрима)…",
            )
        return vod, entry
    return None, None


def _process_vod_segments(
    token: str,
    chat_id: str,
    vod: Path,
    entry: dict | None,
    *,
    labeled: dict,
    probe_limit: int,
    downloader: VodPipelineDownloader,
    registry: list[dict],
    auto_download: bool = True,
) -> int:
    """Drain all scorable segments from one VOD; kick off next download in parallel."""
    from mlbb_vod_adaptive_gate import (
        adaptive_env,
        record_vod_outcome,
        should_notify_soften,
        soft_max_peak_tries,
        streak_from_state,
        telegram_exhaust_notice,
        telegram_soften_notice,
    )

    if auto_download:
        downloader.start_if_idle(registry)
    sent_total = 0
    max_per_vod = int(os.environ.get("MLBB_VOD_MAX_PER_VOD", "0"))
    sig = _vod_signature(vod)
    sent = load_feed_sent()
    vid = vod_youtube_id(vod)
    # Reset per-VOD title gates — leftovers from the previous file poisoned
    # dense/min-tier (wanwan VOD inherited "maniac+savage sun" from prior).
    os.environ.pop("MLBB_VOD_TITLE_MIN_TIER", None)
    os.environ["MLBB_VOD_BANNER_DENSE_SEC"] = "0"
    os.environ.pop("MLBB_BANNER_POS_LIVE_MIN_SIM", None)
    os.environ.pop("MLBB_VOD_BANNER_GAP_SOFT", None)
    os.environ.pop("MLBB_BANNER_DISCOVER_EXCLUDE_SECS", None)
    # Title-aware scan: savage/maniac in title → early dense discover + min banner tier.
    # Set SCAN_TITLE before vod_title_blob — leftover env from the previous VOD
    # was poisoning tier gates (AJ2 inherited ZUK maniac; ZUK inherited OQx savage).
    os.environ["MLBB_VOD_SCAN_TITLE"] = str((entry or {}).get("title") or "")
    try:
        from mlbb_vod_title import title_min_banner_tier, title_promises_kill_streak, vod_title_blob

        title_blob = vod_title_blob(vod, entry)
        title_tier = title_min_banner_tier(title_blob)
        if title_tier > 0:
            os.environ["MLBB_VOD_TITLE_MIN_TIER"] = str(title_tier)
            # Dense 1 Hz only when title promises a high streak — keeps normal VODs fast.
            if title_promises_kill_streak(title_blob) and os.environ.get(
                "MLBB_VOD_TITLE_DENSE_AUTO", "1"
            ) == "1":
                os.environ["MLBB_VOD_BANNER_DENSE_SEC"] = "1"
                # YouTube OCR is often blind; allow owner-pos ref slightly below the
                # live anti-FP floor so title-promised savage/maniac still ship.
                os.environ["MLBB_BANNER_POS_LIVE_MIN_SIM"] = os.environ.get(
                    "MLBB_BANNER_TITLE_LIVE_MIN_SIM", "0.55"
                )
                os.environ.setdefault("MLBB_KILL_BANNER_TITLE_OCR_EVERY", "4")
            log.info(
                "title_gate vod=%s tier_need=%s dense=%s live_sim=%s blob=%s",
                vod.name,
                title_tier,
                os.environ.get("MLBB_VOD_BANNER_DENSE_SEC", "0"),
                os.environ.get("MLBB_BANNER_POS_LIVE_MIN_SIM", ""),
                title_blob[:80],
            )
        else:
            os.environ.pop("MLBB_VOD_TITLE_MIN_TIER", None)
    except Exception as exc:
        log.warning("title_gate skipped: %s", exc)
    state_pre = _load_state()
    streak_in = streak_from_state(state_pre)
    prev_level = int(state_pre.get("last_adaptive_level") or 0)
    active_level = 0
    pool_cache: list[dict] | None = None
    skip_peaks: set[float] = set()
    peak_tries = 0
    send_quota_blocked = False
    labeled_set = set(labeled.keys()) if isinstance(labeled, dict) else set(labeled)
    lead = _vod_lead_sec()

    clear_fast_seeds = None
    skip_fast = bool(entry and entry.get("revive_skip_fast_probe"))
    if skip_fast:
        log.info("revive: skip fast-probe vod=%s", vod.name)
        if entry is not None:
            entry["revive_skip_fast_probe"] = False
    if (not skip_fast) and os.environ.get("MLBB_VOD_FAST_PROBE", "1") == "1":
        from mlbb_vod_fast_scan import (
            apply_fast_probe_seeds,
            clear_fast_probe_seeds,
            vod_fast_combat_check,
        )

        clear_fast_seeds = clear_fast_probe_seeds
        ok_fast, fast_reason, seed_peaks = vod_fast_combat_check(vod, PROFILE)
        if not ok_fast and str(fast_reason) == "fast_probe_too_short":
            # Title-promised savage/maniac shorts must still reach banner discover.
            title_blob = str(
                (entry or {}).get("title")
                or os.environ.get("MLBB_VOD_SCAN_TITLE")
                or ""
            ).lower()
            try:
                from mlbb_vod_title import title_promises_kill_streak

                if title_promises_kill_streak(title_blob):
                    log.info(
                        "fast-probe bypass short title-promised vod=%s",
                        vod.name,
                    )
                    ok_fast, fast_reason, seed_peaks = True, "fast_probe_title_bypass", []
            except Exception:
                pass
        if not ok_fast:
            log.info("fast-skip vod=%s reason=%s", vod.name, fast_reason)
            if entry is None:
                entry = {"id": vid, "path": str(vod), "exhausted": False}
            entry["reject_reason"] = fast_reason
            entry["exhausted"] = True
            record_vod_scan(entry, sent=0, pool_peaks=[], blocked=False)
            state = _load_state()
            record_vod_outcome(state, vod_id=vid, sent=0)
            state["last_adaptive_level"] = 0
            _hard_finish_mlbb_vod(
                state,
                vod,
                vid=vid,
                reason=str(fast_reason or "fast_probe_fail"),
                entry=entry,
            )
            if clear_fast_seeds:
                clear_fast_seeds()
            return 0
        apply_fast_probe_seeds(seed_peaks)

    try:
        with adaptive_env(streak_in) as level:
            active_level = level
            if should_notify_soften(streak_in, level, prev_level=prev_level) and os.environ.get(
                "MLBB_VOD_ADAPTIVE_NOTIFY", "1"
            ) == "1":
                log.warning(
                    "adaptive soften active streak=%s level=%s vod=%s",
                    streak_in,
                    level,
                    vod.name,
                )
                send_message(token, chat_id, telegram_soften_notice(streak_in, level))
            elif level > 0:
                log.warning(
                    "adaptive soften active streak=%s level=%s vod=%s (no tg spam)",
                    streak_in,
                    level,
                    vod.name,
                )

            max_peak_attempts = max_peak_tries(level, game="mlbb", soft_max_fn=soft_max_peak_tries)
            gap = segment_gap_sec("mlbb", soften_level=level)
            # Soften peak/interval gap when a prior scan found banners but gap-blocked them.
            if entry and int(entry.get("banner_gap_retries") or 0) > 0:
                os.environ["MLBB_VOD_BANNER_GAP_SOFT"] = "1"
                soft_peak = float(os.environ.get("MLBB_VOD_BANNER_PEAK_GAP_SEC", "18"))
                gap = min(gap, soft_peak)
                log.info(
                    "banner gap soft vod=%s retries=%s gap=%.0fs",
                    vod.name,
                    entry.get("banner_gap_retries"),
                    gap,
                )
            blocked_ids = labeled_set | sent
            index_segments = load_index().get("segments", [])
            used_peaks = used_peaks_for_vod("mlbb", vid, sent, index_segments)
            # Already-sent peaks must not satisfy discover want=1 early-stop.
            if used_peaks:
                os.environ["MLBB_BANNER_DISCOVER_EXCLUDE_SECS"] = ",".join(
                    str(int(round(float(p)))) for p in used_peaks[:24]
                )
            else:
                os.environ.pop("MLBB_BANNER_DISCOVER_EXCLUDE_SECS", None)

            cached_blocked = False
            if entry and entry.get("last_pool_peaks"):
                cached = peak_values_from_entry(entry)
                if pool_peaks_fully_blocked(
                    cached,
                    used_peaks=used_peaks,
                    gap_sec=gap,
                    blocked_sids=blocked_ids,
                    vod_id=vid,
                    lead_sec=lead,
                ):
                    # Banner hits under a hard gap: force rescan with soft gap instead of delete loop.
                    if (
                        banner_hits_in_entry(entry) > 0
                        and int(entry.get("banner_gap_retries") or 0)
                        < max(0, int(os.environ.get("MLBB_VOD_BANNER_GAP_RETRIES", "2")))
                        and os.environ.get("MLBB_VOD_BANNER_GAP_SOFT", "0") != "1"
                    ):
                        os.environ["MLBB_VOD_BANNER_GAP_SOFT"] = "1"
                        soft_peak = float(os.environ.get("MLBB_VOD_BANNER_PEAK_GAP_SEC", "18"))
                        gap = min(gap, soft_peak)
                        entry["banner_gap_retries"] = int(entry.get("banner_gap_retries") or 0) + 1
                        entry["last_scan_blocked"] = False
                        log.info(
                            "cached peaks blocked but banners=%s — soft-gap rescan vod=%s gap=%.0fs",
                            banner_hits_in_entry(entry),
                            vod.name,
                            gap,
                        )
                        # Invalidate cache so collect re-discovers / re-validates under soft gap.
                        pool_cache = None
                        if entry.get("last_pool_at"):
                            entry["last_pool_at"] = 0
                    else:
                        log.info(
                            "skip highlight rescan — cached peaks blocked vod=%s peaks=%s",
                            vod.name,
                            cached[:4],
                        )
                        record_vod_scan(entry, sent=0, pool_peaks=cached, blocked=True, pool=pool_cache)
                        cached_blocked = True

            while not cached_blocked:
                if max_per_vod > 0 and sent_total >= max_per_vod:
                    log.info("vod cap reached sent=%s max_per_vod=%s vod=%s", sent_total, max_per_vod, vod.name)
                    break
                to_send, pool_cache = _collect_scan_segments(
                    vod,
                    sig,
                    labeled,
                    sent,
                    probe_limit,
                    pool=pool_cache,
                    skip_peaks=skip_peaks,
                    entry=entry,
                )
                pool_peaks = peaks_from_pool(pool_cache) if pool_cache is not None else []
                if not to_send:
                    blocked = False
                    if not pool_peaks:
                        blocked = True
                    elif pool_peaks_fully_blocked(
                        pool_peaks,
                        used_peaks=used_peaks,
                        gap_sec=gap,
                        blocked_sids=blocked_ids,
                        vod_id=vid,
                        lead_sec=lead,
                    ):
                        blocked = True
                    if entry is not None:
                        record_vod_scan(
                            entry,
                            sent=sent_total,
                            pool_peaks=pool_peaks,
                            blocked=blocked,
                            pool=pool_cache,
                        )
                    break
                n, preskip, sblock, permanent = _send_segment_batch(token, chat_id, vod, to_send, sig)
                if n == 0:
                    if to_send and sblock > 0:
                        log.warning("batch blocked from send — keep vod=%s for retry", vod.name)
                        send_quota_blocked = True
                        if entry is not None:
                            record_vod_scan(
                                entry,
                                sent=sent_total,
                                pool_peaks=pool_peaks,
                                blocked=False,
                                pool=pool_cache,
                            )
                        break
                    if to_send and preskip >= len(to_send):
                        for row in to_send:
                            # Skip both window-start and banner peak — they can differ by 10–20s
                            # and previously caused the same failing clip to retry forever.
                            skip_peaks.add(round(float(row.get("start") or 0), 1))
                            skip_peaks.add(round(float(row.get("peak_start", row["start"])), 1))
                            if row.get("banner_sec") is not None:
                                skip_peaks.add(round(float(row["banner_sec"]), 1))
                        peak_tries += 1
                        # banner_missing_min_tier / no double will not improve on re-render.
                        max_tries = 1 if permanent else max_peak_attempts
                        if peak_tries < max_tries:
                            log.warning(
                                "presend rejected peak — try next (%s/%s) vod=%s",
                                peak_tries,
                                max_tries,
                                vod.name,
                            )
                            continue
                        log.warning(
                            "batch presend rejected all — stop vod=%s permanent=%s",
                            vod.name,
                            int(permanent),
                        )
                        if entry is not None:
                            if permanent:
                                entry["reject_reason"] = entry.get("reject_reason") or "presend_banner_floor"
                            record_vod_scan(
                                entry,
                                sent=sent_total,
                                pool_peaks=pool_peaks,
                                blocked=False,
                                pool=pool_cache,
                            )
                        break
                    log.warning("batch had candidates but none sent — stop vod=%s", vod.name)
                    if entry is not None:
                        record_vod_scan(
                            entry,
                            sent=sent_total,
                            pool_peaks=pool_peaks,
                            blocked=False,
                            pool=pool_cache,
                        )
                    break
                sent_total += n
                sent = load_feed_sent()
                blocked_ids = labeled_set | sent
                used_peaks = used_peaks_for_vod("mlbb", vid, sent, index_segments)
                if auto_download:
                    downloader.start_if_idle(registry)
                if entry is not None:
                    record_vod_scan(
                        entry,
                        sent=sent_total,
                        pool_peaks=pool_peaks,
                        blocked=False,
                        pool=pool_cache,
                    )
    finally:
        if clear_fast_seeds:
            clear_fast_seeds()

    state = _load_state()
    if entry:
        _sync_vod_entry_to_state(state, entry, vod)
    state["active_vod"] = vod.name
    scanned = set(state.get("scanned_vods", []))
    scanned.add(vod.name)
    state["scanned_vods"] = sorted(scanned)
    new_streak = record_vod_outcome(state, vod_id=vid, sent=sent_total)
    state["last_adaptive_level"] = active_level
    _save_state(state)

    if sent_total > 0:
        try:
            from mlbb_vod_yield_memory import record_send

            record_send(
                youtube_id=str(vid or ""),
                uploader=str((entry or {}).get("uploader") or ""),
                title=str((entry or {}).get("title") or vod.stem),
                sent=int(sent_total),
            )
        except Exception as exc:
            log.debug("yield send record skipped: %s", exc)

    if sent_total == 0 and not send_quota_blocked:
        if entry:
            entry["zero_send_attempts"] = int(entry.get("zero_send_attempts") or 0) + 1
        # Banner hits found but blocked by prior used-peak gap — soft retry, don't delete.
        if entry and should_retry_banner_gap(entry):
            entry["banner_gap_retries"] = int(entry.get("banner_gap_retries") or 0) + 1
            entry["last_scan_blocked"] = False
            entry["reject_reason"] = "banner_gap_retry"
            state = _load_state()
            _sync_vod_entry_to_state(state, entry, vod)
            _save_state(state)
            log.info(
                "banner gap retry vod=%s hits=%s attempt=%s — keep file",
                vod.name,
                banner_hits_in_entry(entry),
                entry["banner_gap_retries"],
            )
        elif entry and should_mark_vod_exhausted(entry):
            if not entry.get("reject_reason"):
                if not entry.get("last_pool_peaks"):
                    entry["reject_reason"] = "no_combat_peaks"
                elif entry.get("last_scan_blocked"):
                    entry["reject_reason"] = "all_peaks_blocked"
            _record_zero_yield_uploader(entry)
            log.info("exhausted vod=%s adaptive_streak=%s level=%s", vod.name, new_streak, active_level)
            _hard_finish_mlbb_vod(
                state,
                vod,
                vid=vid,
                reason=str(entry.get("reject_reason") or "zero_send"),
                entry=entry,
            )
            if os.environ.get("MLBB_VOD_EXHAUST_NOTIFY", "1") == "1":
                send_message(
                    token,
                    chat_id,
                    telegram_exhaust_notice(vid, level=active_level, streak=new_streak),
                )
        elif _mlbb_reliable_mode() and entry:
            # Reliable: one zero attempt is enough — free disk and move on.
            # Keep only when pool still has SEND-floor banners (double+) to retry.
            # Keep when pool still has montage-eligible banners (double+ or ref singles).
            from mlbb_vod_montage import montage_single_row_ok

            shippable_hits = 0
            for row in entry.get("last_pool_peaks") or []:
                if not isinstance(row, dict):
                    continue
                tier = int(row.get("kill_banner_tier") or 0)
                if tier >= 2 or montage_single_row_ok(row):
                    shippable_hits += 1
            if (
                shippable_hits > 0
                and entry.get("reject_reason") != "presend_banner_floor"
                and os.environ.get("MLBB_VOD_KEEP_BANNER_MISS", "1") == "1"
            ):
                entry["reject_reason"] = entry.get("reject_reason") or "banner_hits_no_send"
                state = _load_state()
                _sync_vod_entry_to_state(state, entry, vod)
                _save_state(state)
                log.info(
                    "reliable zero but shippable banners=%s — keep vod=%s",
                    shippable_hits,
                    vod.name,
                )
            else:
                entry["reject_reason"] = entry.get("reject_reason") or "zero_send_reliable"
                _hard_finish_mlbb_vod(
                    state,
                    vod,
                    vid=vid,
                    reason=str(entry["reject_reason"]),
                    entry=entry,
                )
                log.info("reliable zero — finished vod=%s streak=%s", vod.name, new_streak)
        else:
            log.info("zero send — keep vod=%s for retry (presend/soften) streak=%s", vod.name, new_streak)
    elif sent_total == 0 and send_quota_blocked:
        log.info("send quota blocked — keep vod=%s for next cycle", vod.name)
    else:
        log.info("sent=%s vod=%s (streak reset)", sent_total, vod.name)
        if entry:
            entry["zero_send_attempts"] = 0
        if _mlbb_reliable_mode():
            _hard_finish_mlbb_vod(
                state,
                vod,
                vid=vid,
                reason="sent_ok",
                entry=entry,
            )
        if active_level > 0 and os.environ.get("MLBB_VOD_ADAPTIVE_NOTIFY", "1") == "1":
            send_message(
                token,
                chat_id,
                f"✅ {sent_total} клип(ов) с мягких фильтров (L{active_level}) — возврат к strict после серии",
            )
    return sent_total


def _bootstrap_shorts_exemplars_for_vod() -> dict:
    """Legacy CLIP exemplar bootstrap — off on banner/own-kill live path.

    Yield memory (`mlbb_vod_yield_memory`) drives discovery/pick from 👍/👎
    + own-kill outcomes. Loading 500+ Shorts mp4 into CLIP wastes RAM and
    does not rank banner windows (stage1 CLIP rank is skipped).
    """
    if os.environ.get("MLBB_VOD_OWNER_EXEMPLARS", "0") != "1":
        try:
            from mlbb_vod_yield_memory import summary

            s = summary()
            log.info(
                "vod yield-memory active videos=%s uploaders=%s heroes=%s top_heroes=%s",
                s.get("videos"),
                s.get("uploaders"),
                s.get("heroes"),
                s.get("top_heroes"),
            )
        except Exception:
            log.info("vod yield-memory active (CLIP exemplars off)")
        return {
            "good_exemplars": 0,
            "bad_exemplars": 0,
            "owner_rank": False,
            "yield_memory": True,
        }

    repo = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
    root = repo / "data" / "highlight_exemplars" / "mobile_legends"
    good_n = len(list((root / "good").glob("*.mp4"))) if (root / "good").exists() else 0
    bad_n = len(list((root / "bad").glob("*.mp4"))) if (root / "bad").exists() else 0
    out = {"good_exemplars": good_n, "bad_exemplars": bad_n, "owner_rank": False}
    try:
        from mlbb_calibration_store import owner_rank_enabled, stats as cal_stats, sync_owner_learning

        cal = cal_stats()
        out["owner_rank"] = owner_rank_enabled()
        out["feedback_yes"] = cal.get("feedback_yes", 0)
        out["feedback_no"] = cal.get("feedback_no", 0)
        if os.environ.get("MLBB_VOD_SYNC_OWNER", "1") == "1":
            sync_owner_learning(rescore_limit=0)
    except Exception as exc:
        log.warning("shorts exemplar sync failed: %s", exc)
        try:
            from highlight_scorer import clear_exemplar_cache

            clear_exemplar_cache()
        except Exception:
            pass
    else:
        try:
            from highlight_scorer import clear_exemplar_cache

            clear_exemplar_cache()
        except Exception:
            pass
    log.info(
        "vod owner exemplars good=%s bad=%s owner_rank=%s feedback=%s/%s",
        good_n,
        bad_n,
        out.get("owner_rank"),
        out.get("feedback_yes", "?"),
        out.get("feedback_no", "?"),
    )
    min_good = int(os.environ.get("MLBB_VOD_MIN_GOOD_EXEMPLARS", "50"))
    if good_n < min_good:
        log.warning("few good exemplars (%s < %s) — VOD scoring may be weak", good_n, min_good)
    return out


def _mlbb_reliable_mode() -> bool:
    """Ship videos, not Telegram error spam. Default ON."""
    raw = os.environ.get("MLBB_VOD_RELIABLE")
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip() not in {"0", "false", "False", "no"}


def _apply_mlbb_reliable_runtime() -> None:
    if not _mlbb_reliable_mode():
        return
    defaults = {
        "MLBB_VOD_ADAPTIVE_NOTIFY": "0",
        "MLBB_VOD_EXHAUST_NOTIFY": "0",
        "MLBB_VOD_DISCOVERY_MISS_NOTIFY": "0",
        "MLBB_VOD_DOWNLOAD_NOTIFY": "0",
        "MLBB_VOD_PRESEND_REJECT_NOTIFY": "0",
        # One zero attempt is enough — yield-dead VODs must not loop for hours.
        "MLBB_VOD_MAX_ZERO_ATTEMPTS": "1",
        # .video_bot.env keeps AUTO_DOWNLOAD=0; daily_cycle_runner used to clobber
        # launcher exports → empty/exhausted inbox spun mute for hours.
        "MLBB_VOD_AUTO_DOWNLOAD": "1",
        "MLBB_VOD_AUTO_DOWNLOAD_ON_EMPTY": "1",
        # Keep on disk: deleting own-kill VODs after CLIP-off false-empty was catastrophic.
        "MLBB_VOD_DELETE_EXHAUSTED": "0",
        "MLBB_VOD_KEEP_BANNER_MISS": "0",
        # Hard banner prefilter deletes whole VODs → endless ⚠️ spam.
        # Banner-miss → teamfight/CLIP fallback ships ally junk. Skip the VOD.
        "MLBB_VOD_BANNER_HARD_PREFILTER": "1",
        "MLBB_VOD_BANNER_SKIP_ON_MISS": "1",
        # Reliable = own-kill banner clips; ship 1 strong moment if montage thin.
        "MLBB_VOD_MONTAGE": "1",
        "MLBB_SKIP_MONTAGE": "0",
        "MLBB_VOD_MONTAGE_MIN_CLIPS": "1",
        "MLBB_VOD_MONTAGE_MAX_CLIPS": "4",
        "MLBB_VOD_MONTAGE_MIN_TIER": "single",
        "MLBB_VOD_MONTAGE_ALLOW_SINGLES": "1",
        "MLBB_VOD_MONTAGE_SINGLE_FALLBACK": "1",
        "MLBB_VOD_MONTAGE_GAP_SEC": "40",
        "MLBB_PRESEND_MONTAGE_SINGLE": "1",
        "MLBB_ADAPTIVE_ALLOW_SINGLE": "0",
        "MLBB_BANNER_SEND_MIN_TIER": "double",
        "MLBB_VOD_SEND_ONE": "0",
        # Collect multiple own-kills per VOD when discover finds them (quota speed).
        "MLBB_VOD_SEND_ALL_BANNERS": "1",
        "MLBB_VOD_MAX_PER_VOD": "2",
        "MLBB_KILL_BANNER_DISCOVER_TARGET": "2",
        "MLBB_DISCOVER_SHIP_ON_FIRST": "0",
        "MLBB_VOD_MIN_PEAK_SEC": "20",
        "MLBB_VOD_SEGMENT_GAP_SEC": "40",
        "MLBB_VOD_INTERVAL_GAP_SEC": "10",
        "MLBB_VOD_BANNER_DEDUP_SEC": "20",
        "MLBB_VOD_PRESEND_SKIP_VISUAL_ON_BANNER": "1",
        "MLBB_KILL_BANNER_REQUIRED": "1",
        # Presend OCR hangs; trust discover own-kill + skip CLIP score.
        "MLBB_VOD_BANNER_PRESEND": "0",
        "MLBB_VOD_BANNER_PRESEND_TRUST_DISCOVER": "1",
        "MLBB_BANNER_SKIP_CLIP_SCORE": "1",
        "MLBB_VOD_MOTION_ANCHOR_OK": "0",
        "MLBB_BANNER_POS_INCLUDE_VOD_CROPS": "0",
        "MLBB_BANNER_REF_COLOR_MUL": "1.10",
        "MLBB_BANNER_POS_LIVE_MIN_SIM": "0.55",
        "MLBB_BANNER_REF_ROOT": "/root/content_bot_ml/data/mlbb_kill_banners",
        "CONTENT_BOT_REPO": "/root/content_bot_ml",
        "MLBB_PRESEND_REJECT_RUN": "1",
        "MLBB_PRESEND_MAX_RUN_FRAC": "0.45",
        "MLBB_PRESEND_RUN_TRUST_OWN_KILL": "1",
        "MLBB_PRESEND_REQUIRE_FIGHT_HUD": "1",
        "MLBB_PRESEND_BANNER_CONTEXT": "0",
        "MLBB_PRESEND_RUN_MOTION_MIN": "0.025",
        "MLBB_PRESEND_RUN_MIN_SKILL": "0.008",
        "MLBB_PRESEND_RUN_MIN_MINI": "0.008",
        "MLBB_PRESEND_FIGHT_HUD_MIN": "0.008",
        "MLBB_BANNER_POST_SEC": "1.5",
        "MLBB_FIGHT_POST_SEC": "1.5",
        "MLBB_BANNER_HARD_POST_CUT": "1",
        "MLBB_KILL_BANNER_LEAD_SEC": "8",
        "MLBB_VOD_LEAD_SEC": "8",
        "MLBB_OCR_SINGLE_REQUIRE_HUD": "1",
        "MLBB_PRESEND_OWN_KILL_RECHECK": "1",
        "MLBB_BANNER_REJECT_OCR_SINGLE": "1",
        "MLBB_BANNER_OCR_WEAK_SINGLE": "0",
        "MLBB_ALLOW_OCR_SINGLE_SEND": "0",
        "MLBB_PRESEND_MAX_POST_RUN_FRAC": "0.40",
        "MLBB_BANNER_LIVE_OVERLAY_OCR": "1",
        # Read banner text (RapidOCR+fuzzy). Ref vision alone is not enough.
        "MLBB_BANNER_RAPID_OCR": "1",
        "MLBB_BANNER_REF_REQUIRE_OCR": "1",
        "MLBB_BANNER_OCR_FUZZY_MIN": "0.72",
        # Discover ref floor was 0.35 → jungle farm matched as triple (3lO0).
        "MLBB_BANNER_DISCOVER_POS_LIVE_MIN_SIM": "0.55",
        "MLBB_BANNER_DISCOVER_OWN_KILL_MIN_SIM": "0.55",
        "MLBB_BANNER_DISCOVER_WIKI_MIN_SIM": "0.50",
        "MLBB_BANNER_NEG_POS_MARGIN": "0.06",
        "MLBB_BANNER_NEG_NOT_KILL_MIN": "0.48",
        "MLBB_BANNER_POS_LIVE_MIN_SIM": "0.58",
        # Discover: fight-first spends budget on fight peaks; abort fast on miss.
        "MLBB_KILL_BANNER_DISCOVER_PROBE_AFTER": "4.0",
        "MLBB_KILL_BANNER_DISCOVER_PEAK_FULL_RETRY": "0",
        "MLBB_VOD_TITLE_DENSE_AUTO": "0",
        # Discover: collect single+ anchors; montage ships ref singles.
        # Find several banners so an already-sent kill does not exhaust a 20-kill VOD.
        "MLBB_KILL_BANNER_DISCOVER_MIN_HITS": "1",
        "MLBB_KILL_BANNER_DISCOVER_MERGE_TIER": "1",
        "MLBB_KILL_BANNER_DISCOVER_TITLE_CAP": "1",
        "MLBB_KILL_BANNER_DISCOVER_MAX_SEC": "240",
        "MLBB_KILL_BANNER_DISCOVER_MAX_PROBES": "32",
        "MLBB_KILL_BANNER_DISCOVER_STEP": "1.5",
        "MLBB_USED_YOUTUBE_IDS_CAP": "220",
        "MLBB_VOD_DISCOVERY_REUSE_ZERO_SEND": "1",
        "MLBB_BANNER_DISCOVER_REF_COLOR_MUL": "0.70",
        "MLBB_BANNER_DISCOVER_POS_LIVE_MIN_SIM": "0.55",
        "MLBB_BANNER_DISCOVER_OWN_KILL_MIN_SIM": "0.55",
        "MLBB_BANNER_DISCOVER_WIKI_MIN_SIM": "0.50",
        "MLBB_KILL_BANNER_DENSE_COLOR_MUL": "0.40",
        # Own-kill: HUD portrait vs banner killer (LEFT). Skins covered by HUD match.
        "MLBB_BANNER_OWN_KILL_REQUIRED": "1",
        "MLBB_BANNER_HERO_MATCH": "1",
        "MLBB_BANNER_OWN_HUD_MIN_SIM": "0.19",
        "MLBB_OCR_DOUBLE_REQUIRE_LIVE": "1",
        # Fight-first: fewer peaks, abort on miss, shorter post-peak offsets.
        "MLBB_BANNER_FIGHT_FIRST": "1",
        "MLBB_BANNER_FIGHT_FIRST_PEAKS": "8",
        "MLBB_FIGHT_FIRST_ABORT_ON_MISS": "0",
        "MLBB_KILL_BANNER_DISCOVER_POST_PEAK": "1",
        "MLBB_KILL_BANNER_DISCOVER_POST_PEAK_OFFSETS": "3,6",
        "MLBB_KILL_BANNER_QUICK_BEFORE": "2",
        "MLBB_KILL_BANNER_QUICK_AFTER": "6",
        "MLBB_KILL_BANNER_DISCOVER_PEAK_BUDGET_FRAC": "0.45",
        "MLBB_KILL_BANNER_DISCOVER_PEAK_HINTS": "8",
        # Dense only after miss streak when fight-first is on.
        "MLBB_VOD_BANNER_DENSE_SEC": "0",
        "MLBB_VOD_DISCOVER_ALWAYS_DENSE": "0",
        "MLBB_VOD_DISCOVER_DENSE_AFTER_MISS": "2",
        # Live learning = yield memory (👍/👎 + own-kill), not CLIP exemplar mp4s.
        "MLBB_VOD_YIELD_MEMORY_ENABLED": "1",
        "MLBB_VOD_OWNER_EXEMPLARS": "0",
        "HIGHLIGHT_USE_OWNER_ANCHORS": "0",
        "HIGHLIGHT_CLIP_DISABLED": "1",
        "HIGHLIGHT_MLBB_BANNER_CLIP_MIN": "0.0",
        "MLBB_KILL_SCAN_SKIP_OCR": "1",
        # Bounded discover OCR only — not presend (presend hangs on tesseract).
        "MLBB_KILL_DISCOVER_ALLOW_OCR": "1",
        "MLBB_TESSERACT_TIMEOUT_SEC": "4",
        # OCR on spike probes hung for minutes (OQx); ref-first is enough for quota speed.
        "MLBB_KILL_BANNER_DISCOVER_OCR_SPIKES": "0",
        "MLBB_KILL_BANNER_FORCE_OCR_EVERY": "0",
        "MLBB_KILL_BANNER_OCR_WIDE": "0",
        "MLBB_DISCOVER_OCR_CALL_BUDGET": "10",
        "MLBB_STAGE1_SKIP_CLIP_RANK": "1",
        "MLBB_STAGE1_SKIP_INTELLICLIP": "1",
        "INTELLICLIP_STAGE1": "0",
        "MLBB_VOD_SKIP_TANK_SUPPORT": "1",
        # Cooldowns: do not sit idle 30–45 min after a miss when quota is open.
        "MLBB_VOD_PRESEND_COOLDOWN_SEC": "120",
        "MLBB_VOD_ZERO_SEND_COOLDOWN_SEC": "180",
        "MLBB_VOD_SCAN_COOLDOWN_SEC": "300",
        # Quality floor so OCR-blind soften cannot ship farming junk.
        "MLBB_RULE_COMBAT_MIN": "0.80",
        "HIGHLIGHT_MLBB_AUTO_CLIP_MIN": "0.10",
        "VIRAL_MLBB_CLIP_HOOK_MIN": "0.15",
        "MLBB_TEAMFIGHT_MIN_SCORE": "0.40",
        "MLBB_MOTION_PEAK_MAX": "6",
        # Faster analyze so VODs turn over in ~1–2 min, not 3–4.
        "SMART_SAMPLE_FPS": "2.0",
        "SMART_LONG_SAMPLE_FPS": "2.0",
        "SMART_ANALYSIS_DETAIL": "fast",
        "SMART_LONG_ANALYSIS_MAX_FPS": "2.0",
    }
    force = {
        "MLBB_VOD_ADAPTIVE_NOTIFY",
        "MLBB_VOD_EXHAUST_NOTIFY",
        "MLBB_VOD_DISCOVERY_MISS_NOTIFY",
        "MLBB_VOD_DOWNLOAD_NOTIFY",
        "MLBB_VOD_PRESEND_REJECT_NOTIFY",
        "MLBB_VOD_BANNER_HARD_PREFILTER",
        "MLBB_VOD_BANNER_SKIP_ON_MISS",
        "MLBB_VOD_MAX_ZERO_ATTEMPTS",
        "MLBB_VOD_AUTO_DOWNLOAD",
        "MLBB_VOD_AUTO_DOWNLOAD_ON_EMPTY",
        "MLBB_VOD_DELETE_EXHAUSTED",
        "MLBB_VOD_KEEP_BANNER_MISS",
        "MLBB_VOD_MONTAGE",
        "MLBB_SKIP_MONTAGE",
        "MLBB_VOD_MONTAGE_MIN_CLIPS",
        "MLBB_VOD_MONTAGE_MAX_CLIPS",
        "MLBB_VOD_MONTAGE_MIN_TIER",
        "MLBB_VOD_MONTAGE_ALLOW_SINGLES",
        "MLBB_VOD_MONTAGE_SINGLE_FALLBACK",
        "MLBB_PRESEND_MONTAGE_SINGLE",
        "MLBB_VOD_SEND_ONE",
        "MLBB_VOD_SEND_ALL_BANNERS",
        "MLBB_VOD_MAX_PER_VOD",
        "MLBB_KILL_BANNER_DISCOVER_TARGET",
        "MLBB_DISCOVER_SHIP_ON_FIRST",
        "MLBB_VOD_SEGMENT_GAP_SEC",
        "MLBB_VOD_INTERVAL_GAP_SEC",
        "MLBB_VOD_BANNER_DEDUP_SEC",
        "MLBB_VOD_PRESEND_SKIP_VISUAL_ON_BANNER",
        "MLBB_KILL_BANNER_REQUIRED",
        "MLBB_VOD_BANNER_PRESEND",
        "MLBB_VOD_MOTION_ANCHOR_OK",
        "MLBB_BANNER_POS_INCLUDE_VOD_CROPS",
        "MLBB_BANNER_REF_COLOR_MUL",
        "MLBB_BANNER_POS_LIVE_MIN_SIM",
        "MLBB_BANNER_REF_ROOT",
        "CONTENT_BOT_REPO",
        "MLBB_RULE_COMBAT_MIN",
        "HIGHLIGHT_MLBB_AUTO_CLIP_MIN",
        "VIRAL_MLBB_CLIP_HOOK_MIN",
        "MLBB_TEAMFIGHT_MIN_SCORE",
        "MLBB_MOTION_PEAK_MAX",
        "SMART_SAMPLE_FPS",
        "SMART_LONG_SAMPLE_FPS",
        "SMART_ANALYSIS_DETAIL",
        "SMART_LONG_ANALYSIS_MAX_FPS",
        "MLBB_VOD_BANNER_PRESEND_TRUST_DISCOVER",
        "MLBB_BANNER_SKIP_CLIP_SCORE",
        "MLBB_BANNER_REJECT_OCR_SINGLE",
        "MLBB_BANNER_OCR_WEAK_SINGLE",
        "MLBB_BANNER_HARD_POST_CUT",
        "MLBB_BANNER_POST_SEC",
        "MLBB_FIGHT_POST_SEC",
        "MLBB_KILL_BANNER_LEAD_SEC",
        "MLBB_VOD_LEAD_SEC",
        "MLBB_OCR_SINGLE_REQUIRE_HUD",
        "MLBB_PRESEND_OWN_KILL_RECHECK",
        "MLBB_ALLOW_OCR_SINGLE_SEND",
        "MLBB_ADAPTIVE_ALLOW_SINGLE",
        "MLBB_BANNER_SEND_MIN_TIER",
        "MLBB_PRESEND_MAX_POST_RUN_FRAC",
        "MLBB_BANNER_LIVE_OVERLAY_OCR",
        "MLBB_BANNER_RAPID_OCR",
        "MLBB_BANNER_REF_REQUIRE_OCR",
        "MLBB_BANNER_OCR_FUZZY_MIN",
        "MLBB_PRESEND_MAX_RUN_FRAC",
        "MLBB_KILL_BANNER_QUICK_AFTER",
        "MLBB_KILL_BANNER_DISCOVER_PEAK_BUDGET_FRAC",
        "MLBB_KILL_BANNER_DISCOVER_PROBE_AFTER",
        "MLBB_KILL_BANNER_DISCOVER_PEAK_FULL_RETRY",
        "MLBB_KILL_BANNER_DISCOVER_MIN_HITS",
        "MLBB_VOD_TITLE_DENSE_AUTO",
        "MLBB_KILL_BANNER_DISCOVER_MERGE_TIER",
        "MLBB_KILL_BANNER_DISCOVER_TITLE_CAP",
        "MLBB_VOD_MIN_PEAK_SEC",
        "MLBB_KILL_BANNER_DISCOVER_MAX_SEC",
        "MLBB_KILL_BANNER_DISCOVER_STEP",
        "MLBB_VOD_DISCOVER_ALWAYS_DENSE",
        "MLBB_BANNER_DISCOVER_REF_COLOR_MUL",
        "MLBB_BANNER_DISCOVER_POS_LIVE_MIN_SIM",
        "MLBB_BANNER_DISCOVER_OWN_KILL_MIN_SIM",
        "MLBB_BANNER_DISCOVER_WIKI_MIN_SIM",
        "MLBB_BANNER_NEG_POS_MARGIN",
        "MLBB_BANNER_NEG_NOT_KILL_MIN",
        "MLBB_BANNER_POS_LIVE_MIN_SIM",
        "MLBB_KILL_BANNER_DENSE_COLOR_MUL",
        "MLBB_BANNER_OWN_KILL_REQUIRED",
        "MLBB_BANNER_HERO_MATCH",
        "MLBB_BANNER_OWN_HUD_MIN_SIM",
        "MLBB_OCR_DOUBLE_REQUIRE_LIVE",
        "MLBB_BANNER_FIGHT_FIRST",
        "MLBB_BANNER_FIGHT_FIRST_PEAKS",
        "MLBB_FIGHT_FIRST_ABORT_ON_MISS",
        "MLBB_KILL_BANNER_DISCOVER_POST_PEAK",
        "MLBB_KILL_BANNER_DISCOVER_POST_PEAK_OFFSETS",
        "MLBB_KILL_BANNER_QUICK_BEFORE",
        "MLBB_KILL_BANNER_QUICK_AFTER",
        "MLBB_KILL_BANNER_DISCOVER_PEAK_BUDGET_FRAC",
        "MLBB_KILL_BANNER_DISCOVER_PEAK_HINTS",
        "MLBB_KILL_BANNER_DISCOVER_MAX_PROBES",
        "MLBB_VOD_BANNER_DENSE_SEC",
        "MLBB_VOD_DISCOVER_ALWAYS_DENSE",
        "MLBB_VOD_DISCOVER_DENSE_AFTER_MISS",
        "MLBB_VOD_TITLE_DENSE_AUTO",
        "MLBB_VOD_YIELD_MEMORY_ENABLED",
        "MLBB_VOD_OWNER_EXEMPLARS",
        "HIGHLIGHT_USE_OWNER_ANCHORS",
        "HIGHLIGHT_CLIP_DISABLED",
        "HIGHLIGHT_MLBB_BANNER_CLIP_MIN",
        "MLBB_BANNER_SKIP_CLIP_SCORE",
        "MLBB_KILL_SCAN_SKIP_OCR",
        "MLBB_KILL_DISCOVER_ALLOW_OCR",
        "MLBB_TESSERACT_TIMEOUT_SEC",
        "MLBB_KILL_BANNER_DISCOVER_OCR_SPIKES",
        "MLBB_KILL_BANNER_FORCE_OCR_EVERY",
        "MLBB_KILL_BANNER_OCR_WIDE",
        "MLBB_DISCOVER_OCR_CALL_BUDGET",
        "MLBB_STAGE1_SKIP_CLIP_RANK",
        "MLBB_STAGE1_SKIP_INTELLICLIP",
        "INTELLICLIP_STAGE1",
        "MLBB_VOD_PRESEND_COOLDOWN_SEC",
        "MLBB_VOD_ZERO_SEND_COOLDOWN_SEC",
        "MLBB_VOD_SCAN_COOLDOWN_SEC",
        "MLBB_VOD_SKIP_TANK_SUPPORT",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
        if key in force:
            os.environ[key] = value


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _apply_mlbb_reliable_runtime()

    if os.environ.get("MLBB_EVAL_ONLY", "0") == "1":
        from mlbb_learning_first import eval_transition_gate

        report = eval_transition_gate()
        dry = report.get("dry_run", {})
        print(
            f"eval_only all_pass={report['all_pass']} "
            f"holdout={report['holdout'].get('precision')} "
            f"dry_rejected={dry.get('rejected')}/{dry.get('tested')}"
        )
        return 0 if report["all_pass"] else 1

    if os.environ.get("MLBB_ONLY_MODE", "1") != "1":
        print("SKIP: MLBB_ONLY_MODE not set")
        return 0

    log.info("mlbb feed start reliable=%s", int(_mlbb_reliable_mode()))

    from mlbb_pipeline_health import log_pipeline_health

    log_pipeline_health(state=_load_state())

    seg_sec = os.environ.get("MLBB_VOD_SEGMENT_SEC", "15")
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    if os.environ.get("MLBB_VOD_OWNER_EXEMPLARS", "0") == "1":
        os.environ["HIGHLIGHT_USE_OWNER_ANCHORS"] = "1"
        os.environ.setdefault("HIGHLIGHT_CLIP_DISABLED", "0")
    else:
        os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
        os.environ.setdefault("MLBB_VOD_YIELD_MEMORY_ENABLED", "1")
    os.environ.setdefault("STRICT_PROBE_LIMIT", os.environ.get("MLBB_VOD_PROBE_LIMIT", "50"))
    os.environ.setdefault("OWNER_PREVIEW_REQUIRED", "0")
    os.environ.setdefault("MLBB_VOD_NO_CROP", "1")
    os.environ.setdefault("MLBB_VOD_LANDSCAPE", "1")
    os.environ.setdefault("MLBB_VOD_VARIABLE_LENGTH", "1")
    os.environ["LOGO_FILE"] = "/nonexistent/mlbb_calibration_no_logo.png"
    os.environ.setdefault("MLBB_VOD_SEGMENT_SEC", seg_sec)
    os.environ.setdefault("HIGHLIGHT_WINDOW_SEC", seg_sec)

    for key, val in strict_peak_env(PROFILE).items():
        os.environ[key] = val

    env = {**os.environ, **load_env(ENV_PATH)}
    for key in (
        "MLBB_VOD_NO_CROP",
        "MLBB_VOD_LANDSCAPE",
        "MLBB_VOD_VARIABLE_LENGTH",
    ):
        if key in env:
            os.environ[key] = str(env[key])
    # Re-force after .video_bot.env — do not let stale OWNER_EXEMPLARS=1 revive CLIP.
    _apply_mlbb_reliable_runtime()
    if os.environ.get("MLBB_VOD_OWNER_EXEMPLARS", "0") != "1":
        os.environ["HIGHLIGHT_CLIP_DISABLED"] = "1"
        os.environ["MLBB_STAGE1_SKIP_INTELLICLIP"] = "1"
        os.environ["INTELLICLIP_STAGE1"] = "0"
    _bootstrap_shorts_exemplars_for_vod()
    if env.get("MLBB_SEND_ENABLED", "1") == "1":
        from mlbb_learning_first import set_transition_passed

        set_transition_passed(True)
        log.info("sends enabled MLBB_SEND_ENABLED=1")
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1

    with _feed_singleton_lock(blocking=False) as acquired:
        if not acquired:
            log.warning("another mlbb_vod_segment_feed already running — exit")
            return 0
        return _run_feed(env, token, chat_id)


def _run_feed(env: dict[str, str], token: str, chat_id: str) -> int:
    if os.environ.get("DAILY_GAME_CYCLE_ENABLED", "0") == "1":
        from daily_game_cycle import active_game, can_send_for_game, reset_if_new_day

        reset_if_new_day()
        if active_game() != "mlbb":
            log.info("daily cycle: active_game=%s — mlbb feed idle", active_game())
            return 0
        ok_mlbb, why = can_send_for_game("mlbb", 1)
        if not ok_mlbb:
            log.info("daily cycle mlbb blocked: %s", why)
            return 0

    labeled = labeled_ids()
    probe_limit = int(os.environ.get("MLBB_VOD_PROBE_LIMIT", "12"))
    auto_download = os.environ.get("MLBB_VOD_AUTO_DOWNLOAD", "1") == "1"
    max_min = float(os.environ.get("MLBB_VOD_PIPELINE_MAX_MIN", "360"))
    if max_min <= 0:
        max_min = 24 * 60.0
    deadline = time.time() + max_min * 60
    max_vods = int(os.environ.get("MLBB_VOD_PIPELINE_MAX_VODS", "4"))
    if max_vods <= 0:
        max_vods = 10_000

    # Empty inbox + AUTO_DOWNLOAD=0 → hours of mute. Pull hold_quota first.
    promoted = _promote_hold_quota_to_inbox()
    if promoted:
        log.info("promoted %s vod(s) from hold_quota", promoted)

    registry = _ensure_registry(env)
    if promoted:
        if _unexhaust_inbox_paths(registry):
            state = _load_state()
            state["vods"] = registry
            _save_state(state)
    if _auto_exhaust_oversized(registry):
        state = _load_state()
        state["vods"] = registry
        _save_state(state)
    inbox_ready = sum(1 for p in INBOX.glob("yt_*.mp4") if p.stat().st_size > 1_000_000)
    pickable = _inbox_pickable_count(registry)
    # Exhausted mp4s still count as "inbox ready" and used to block downloads
    # forever (discovery_empty_streak stayed 0 → no revive). Treat unpickable
    # inbox like empty.
    if pickable == 0 and inbox_ready > 0:
        revived = _revive_exhausted_inbox_candidates(registry, force=True)
        if revived:
            pickable = _inbox_pickable_count(registry)
            log.info("force-revived %s exhausted inbox vod(s); pickable=%s", revived, pickable)
    if (
        not auto_download
        and pickable == 0
        and os.environ.get("MLBB_VOD_AUTO_DOWNLOAD_ON_EMPTY", "1") == "1"
    ):
        auto_download = True
        log.warning(
            "inbox unpickable (ready=%s pickable=0) — enabling AUTO_DOWNLOAD for this run",
            inbox_ready,
        )
    downloader = VodPipelineDownloader(env)
    total_sent = 0
    vods_done = 0
    notified_download = False

    while time.time() < deadline and vods_done < max_vods:
        download_notify = (
            not notified_download
            and os.environ.get("MLBB_VOD_DOWNLOAD_NOTIFY", "1") == "1"
        )
        vod, entry = _resolve_next_vod(
            env,
            registry,
            downloader,
            auto_download=auto_download,
            token=token,
            chat_id=chat_id,
            notify=download_notify,
        )
        if vod is None:
            _note_discovery_empty(kept=0)
            if (
                not notified_download
                and auto_download
                and os.environ.get("MLBB_VOD_DISCOVERY_MISS_NOTIFY", "0") == "1"
            ):
                send_message(token, chat_id, "⚠️ Не нашёл новый MLBB стрим. Повторю на следующем запуске.")
            else:
                log.info("discovery miss (notify muted)")
            try:
                from daily_game_cycle import maybe_skip_on_discovery_miss

                if maybe_skip_on_discovery_miss("mlbb"):
                    log.warning("daily cycle skip mlbb after discovery misses")
            except Exception as exc:
                log.warning("discovery-miss skip failed: %s", exc)
            break
        try:
            from daily_game_cycle import clear_discovery_miss

            clear_discovery_miss("mlbb")
        except Exception:
            pass
        _note_discovery_empty(kept=1)
        notified_download = True

        n = _process_vod_segments(
            token,
            chat_id,
            vod,
            entry,
            labeled=labeled,
            probe_limit=probe_limit,
            downloader=downloader,
            registry=registry,
            auto_download=auto_download,
        )
        total_sent += n
        vods_done += 1
        registry[:] = _ensure_registry(env)

    print(f"pipeline done sent={total_sent} vods={vods_done}")
    return 0 if total_sent > 0 or vods_done > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
