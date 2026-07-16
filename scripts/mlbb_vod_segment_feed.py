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
    max_peak_tries,
    peak_values_from_entry,
    peaks_from_pool,
    pool_cache_valid,
    pool_peaks_fully_blocked,
    record_vod_scan,
    should_mark_vod_exhausted,
    invalidate_pool_cache,
    note_zero_send_session,
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


def _last_send_age_sec() -> float:
    try:
        sent_path = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb")) / "vod_segment_feed_sent.json"
        if not sent_path.exists():
            return 10**9
        data = json.loads(sent_path.read_text(encoding="utf-8"))
        ts = str(data.get("updated_at") or "")
        if not ts:
            return 10**9
        return max(0.0, time.time() - time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S")))
    except Exception:
        return 10**9


def _mlbb_relax_overrides(zero_send_streak: int, *, adaptive_streak: int = 0) -> dict[str, str]:
    """Return env overrides for throughput when we have many zero-yield VODs.

    Uses persisted adaptive streak + silence since last Telegram send, not just
    the in-memory streak of the current pipeline process.
    """
    relax_after = int(os.environ.get("MLBB_RELAX_AFTER_ZERO_VODS", "3"))
    silence_sec = float(os.environ.get("MLBB_THROUGHPUT_SILENCE_SEC", "1800"))
    silent = _last_send_age_sec() >= silence_sec
    if zero_send_streak < relax_after and adaptive_streak < relax_after and not silent:
        return {}
    return {
        "MLBB_VOD_QUALITY_MODE": os.environ.get("MLBB_VOD_QUALITY_MODE_RELAX", "0"),
        "MLBB_VOD_MIN_CLIP_SCORE": os.environ.get("MLBB_VOD_MIN_CLIP_SCORE_RELAX", "0.02"),
        "MLBB_BANNER_MIN_HOOK": os.environ.get("MLBB_BANNER_MIN_HOOK_RELAX", "0.03"),
        "MLBB_FEEDBACK_GATE": os.environ.get("MLBB_FEEDBACK_GATE_RELAX", "0"),
        "MLBB_VOD_DISABLE_SOFTEN": "0",
    }

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


def _vod_max_process_sec() -> float:
    return max(300.0, float(os.environ.get("MLBB_VOD_MAX_PROCESS_MIN", "50")) * 60.0)


def _configure_banner_scan_policy(title_tier: int, *, priority_rescan: bool = False) -> bool:
    """Reset leaked per-VOD OCR env and enable dense scan only for Maniac/Savage."""
    os.environ["MLBB_VOD_BANNER_DENSE_SEC"] = "0"
    os.environ["MLBB_KILL_BANNER_DISCOVER_STEP"] = os.environ.get(
        "MLBB_KILL_BANNER_SPARSE_STEP", "3"
    )
    os.environ["MLBB_KILL_BANNER_DISCOVER_MAX_SEC"] = os.environ.get(
        "MLBB_KILL_BANNER_SPARSE_MAX_SEC", "180"
    )
    os.environ["MLBB_KILL_BANNER_TIMESTEP_SAMPLES"] = os.environ.get(
        "MLBB_KILL_BANNER_SPARSE_SAMPLES", "48"
    )
    dense = int(title_tier) >= 4 or priority_rescan
    if dense:
        os.environ["MLBB_VOD_BANNER_DENSE_SEC"] = "1"
        os.environ["MLBB_KILL_BANNER_DISCOVER_STEP"] = "1"
        os.environ["MLBB_KILL_BANNER_DISCOVER_MAX_SEC"] = os.environ.get(
            "MLBB_KILL_BANNER_DENSE_MAX_SEC", "360"
        )
    elif int(title_tier) >= 2:
        # Double/Triple titles get wider recall than generic VODs without full dense OCR.
        os.environ["MLBB_KILL_BANNER_DISCOVER_STEP"] = os.environ.get(
            "MLBB_KILL_BANNER_TITLE_STEP", "2"
        )
        os.environ["MLBB_KILL_BANNER_DISCOVER_MAX_SEC"] = os.environ.get(
            "MLBB_KILL_BANNER_TITLE_MAX_SEC", "240"
        )
        os.environ["MLBB_KILL_BANNER_TIMESTEP_SAMPLES"] = os.environ.get(
            "MLBB_KILL_BANNER_TITLE_SAMPLES", "64"
        )
    return dense


def _vod_min_peak_sec(vod: Path | None = None) -> float:
    """Skip laning/spawn — fights usually after ~5–7 min on full VODs."""
    if vod is not None:
        try:
            from mlbb_vod_title import title_min_banner_tier, title_scan_start_sec, vod_title_blob

            blob = vod_title_blob(vod)
            if title_min_banner_tier(blob) >= 2:
                dur = _ffprobe_duration(vod)
                early = title_scan_start_sec(blob, dur)
                if early is not None:
                    return max(0.0, early)
        except Exception:
            pass
    base = float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "420"))
    if vod is None:
        return base
    dur = _ffprobe_duration(vod)
    if dur <= 240:
        return min(base, 45.0)
    if dur <= 480:
        return min(base, 120.0)
    if dur <= 900:
        return min(base, 180.0)
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
    path: Path,
    *,
    title: str = "",
    uploader: str = "",
    search_query: str = "",
    exhausted: bool = False,
) -> dict:
    vid = vod_youtube_id(path)
    return {
        "id": vid,
        "path": str(path),
        "title": title or path.name,
        "uploader": uploader,
        "search_query": search_query,
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


def _ensure_registry(env: dict[str, str]) -> list[dict]:
    state = _load_state()
    registry: list[dict] = list(state.get("vods", []))
    if _repair_registry_ids(registry):
        log.info("repaired registry youtube ids")
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
            if LONG_VOD_TITLE_RE.search(title):
                continue
            if vid == "E4Dsp53yvv4" or MLBB_TITLE_RE.search(title):
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


def _pick_available_vod(registry: list[dict]) -> dict | None:
    from mlbb_source_yield import source_rank_adjustment, video_rank_adjustment

    target = _vod_target_dur_sec()
    exhausted_ids = {
        str(row.get("id") or "")
        for row in registry
        if row.get("exhausted") and row.get("id")
    }
    ranked: list[tuple[int, int, float, float, float, float, dict]] = []
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
        if vid:
            seen_ids.add(vid)
        scanned = float(row.get("last_scan_at") or 0)
        zero_sessions = int(row.get("zero_send_sessions") or 0)
        priority = 1 if row.get("title_rescan_priority") and zero_sessions < 2 else 0
        learned_source = source_rank_adjustment(row) + video_rank_adjustment(vid)
        ranked.append(
            (
                -priority,
                1 if scanned else 0,
                -learned_source,
                scanned,
                abs(dur - target),
                dur,
                row,
            )
        )
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]))
    pick = ranked[0][6]
    dur = ranked[0][5]
    log.info(
        "pick vod id=%s dur_min=%.0f target_min=%.0f scanned=%s",
        pick.get("id", ""),
        dur / 60,
        target / 60,
        bool(ranked[0][1]),
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


def _zero_yield_uploaders() -> set[str]:
    """Legacy compatibility; source-yield decisions now require repeated evidence."""
    return set()


def _record_zero_yield_uploader(meta: dict | None) -> None:
    """Deprecated one-strike block; outcomes are stored in mlbb_source_yield."""
    if meta:
        log.info("zero-yield source recorded without one-strike block: %s", meta.get("uploader", ""))


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
    from mlbb_hero_roles import passes_vod_hero_gate
    from mlbb_source_yield import source_rank_adjustment, uploader_hard_blocked
    from mlbb_correspondence import corresponds_to_mlbb_search

    min_sec = _effective_vod_min_sec()
    max_sec = _vod_max_sec()
    target = _vod_target_dur_sec()
    search_delay = float(os.environ.get("MLBB_VOD_SEARCH_DELAY", "5"))
    search_limit = int(os.environ.get("MLBB_VOD_SEARCH_LIMIT", "25"))
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
        for meta in batch:
            meta.setdefault("search_query", query)
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
            continue
        if not passes_mlbb_game_title(title):
            skipped["not_mlbb"] = skipped.get("not_mlbb", 0) + 1
            continue
        if dur < eff_min or dur > max_sec:
            skipped["duration"] = skipped.get("duration", 0) + 1
            continue
        hero_ok, hero_reason = passes_vod_hero_gate(title)
        if not hero_ok:
            skipped["support_hero"] = skipped.get("support_hero", 0) + 1
            log.info("skip support title id=%s reason=%s title=%s", vid, hero_reason, title[:70])
            continue
        correspondence_ok, correspondence_reason = corresponds_to_mlbb_search(
            title=title,
            search_query=str(meta.get("search_query") or ""),
        )
        if not correspondence_ok:
            skipped["no_correspondence"] = skipped.get("no_correspondence", 0) + 1
            log.info(
                "skip correspondence id=%s reason=%s title=%s",
                vid,
                correspondence_reason,
                title[:70],
            )
            continue
        if uploader_hard_blocked(meta):
            skipped["low_yield_uploader"] = skipped.get("low_yield_uploader", 0) + 1
            continue
        if not passes_mlbb_vod_filters(meta):
            skipped["bad_title"] = skipped.get("bad_title", 0) + 1
            log.info("skip bad title id=%s %s", vid, title[:70])
            continue
        if not passes_upload_freshness(meta, max_age_days=int(search_params.get("max_age_days") or vod_max_age_days(env))):
            skipped["stale_upload"] = skipped.get("stale_upload", 0) + 1
            continue
        out.append(meta)
    if skipped:
        log.info("discovery filtered raw=%s kept=%s skipped=%s", len(raw), len(out), skipped)
    out.sort(
        key=lambda m: (
            -(rank_mlbb_vod_candidate(m, target_dur_sec=target) + source_rank_adjustment(m)),
            -(int(parse_upload_date_ymd(str(m.get("upload_date") or "")) or 0)),
            abs(float(m.get("duration") or 0) - target),
        )
    )
    if out:
        top = out[0]
        log.info(
            "discovery pick id=%s score=%.2f source=%.2f query=%s dur_min=%.0f title=%s",
            top.get("id"),
            rank_mlbb_vod_candidate(top, target_dur_sec=target),
            source_rank_adjustment(top),
            str(top.get("search_query") or "")[:60],
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
    if expected.exists() and expected.stat().st_size > 0:
        return expected
    matches = [p for p in INBOX.glob(f"yt_{vid}*.mp4") if p.stat().st_size > 0]
    if matches:
        return max(matches, key=lambda p: p.stat().st_mtime)
    files = [p for p in INBOX.glob("yt_*.mp4") if p.stat().st_size > 0]
    if not files:
        raise RuntimeError(f"yt-dlp produced no mp4 for {url} (id={vid})")
    raise RuntimeError(f"yt-dlp did not create expected file {expected} for {url}")


def _download_new_mlbb_vod(env: dict[str, str], registry: list[dict], *, throttled: bool = True) -> Path | None:
    state = _load_state()
    used = set(state.get("used_youtube_ids", []))
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
        search_query=str(pick.get("search_query") or "")[:240],
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
        send_as_file = os.environ.get("VOD_CALIBRATION_SEND_AS_FILE", "0") == "1"
        # Inline sendVideo (preview + 👍/👎) — default. Document only when forced or >20MB after compress.
        if deliver.stat().st_size <= TELEGRAM_MAX_BYTES:
            sent = send_video_file(token, chat_id, deliver, caption, reply_markup=markup)
        elif send_as_file and path.stat().st_size <= TELEGRAM_DOCUMENT_MAX_BYTES:
            fname = f"{game.upper()}_{seg_id}.mp4"
            sent = send_hq_files(
                token,
                chat_id,
                path,
                f"{caption}\n📁 файл (без пережатия Telegram)",
                reply_markup=markup,
                filename=fname,
            )
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
    return float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))


def _apply_lead_start(start: float) -> float:
    return max(0.0, start - _vod_lead_sec())


def _normalize_clip(clip: dict, vod: Path) -> dict:
    # Prefer OCR banner time over highlight window start (POV + fight bounds).
    peak = float(clip.get("banner_sec") or clip.get("peak_start") or clip.get("start", 0))
    tier_known = int(clip.get("kill_banner_tier") or 0)
    if (
        clip.get("anchor") == "kill_banner"
        and clip.get("kill_banner")
        and tier_known >= int(os.environ.get("MLBB_VOD_TITLE_MIN_TIER", "0") or 0)
        and tier_known >= 2
    ):
        from mlbb_fight_segment import _analysis_for, detect_fight_bounds
        from mlbb_kill_banner import bounds_from_banner

        banner_sec = float(clip.get("banner_sec") or peak)
        analysis = _analysis_for(vod)
        file_dur = float(analysis.get("duration") or 0.0)
        if file_dur <= 0:
            file_dur = _ffprobe_duration(vod)
        fight_start, fight_end, fight_dur = detect_fight_bounds(vod, banner_sec)
        start, end, dur = bounds_from_banner(
            banner_sec,
            file_dur,
            fight_start=fight_start,
            fight_end=fight_end,
            banner_tier=tier_known,
        )
        return {
            **clip,
            "start": start,
            "peak_start": banner_sec,
            "banner_sec": banner_sec,
            "fight_end": end,
            "source_path": str(vod),
            "source_index": 0,
            "input_duration": dur,
            "output_duration": dur,
            "speed": 1.0,
            "anchor": "kill_banner",
            "kill_banner": clip.get("kill_banner"),
            "kill_banner_tier": tier_known,
        }
    if os.environ.get("MLBB_VOD_VARIABLE_LENGTH", "1") == "1":
        from mlbb_fight_segment import _analysis_for, detect_fight_bounds
        from mlbb_kill_banner import resolve_fight_bounds

        analysis = _analysis_for(vod)
        file_dur = float(analysis.get("duration") or 0.0)
        if file_dur <= 0:
            file_dur = _ffprobe_duration(vod)
        resolved = resolve_fight_bounds(vod, peak, file_dur)
        if resolved is None:
            from mlbb_kill_banner import _motion_anchor_ok

            if _motion_anchor_ok():
                fight_start, fight_end, fight_dur = detect_fight_bounds(vod, peak)
                min_fight = float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7"))
                if fight_dur >= min_fight:
                    resolved = (
                        fight_start,
                        fight_end,
                        fight_dur,
                        {"anchor": "motion", "banner_sec": peak},
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
        prior_tier = int(clip.get("kill_banner_tier") or 0)
        prior_label = str(clip.get("kill_banner") or "")
        meta_tier = int(meta.get("kill_banner_tier") or 0)
        tier_out = max(prior_tier, meta_tier)
        label_out = prior_label if prior_tier >= meta_tier and prior_label else meta.get("kill_banner")
        source_out = str(
            clip.get("kill_banner_source")
            or meta.get("kill_banner_source")
            or meta.get("banner_source")
            or ""
        )
        if (
            tier_out >= 2
            and os.environ.get("MLBB_BANNER_SEND_STRICT", "1") == "1"
            and not source_out.endswith("_verified")
        ):
            try:
                from gameplay_gate import _read_frame_at
                from mlbb_banner_calibration_gate import check_banner_frame_passes

                frame = _read_frame_at(vod, banner_sec)
                proof_ok, proof_reason = check_banner_frame_passes(
                    frame,
                    tier=tier_out,
                )
                if not proof_ok:
                    return {
                        **clip,
                        "start": start,
                        "peak_start": banner_sec,
                        "input_duration": 0.0,
                        "output_duration": 0.0,
                        "banner_reject": proof_reason,
                        "source_path": str(vod),
                        "source_index": 0,
                        "speed": 1.0,
                    }
                source_out = f"{source_out or 'ocr'}_verified"
            except Exception:
                return {
                    **clip,
                    "start": start,
                    "peak_start": banner_sec,
                    "input_duration": 0.0,
                    "output_duration": 0.0,
                    "banner_reject": "banner_visual_proof_error",
                    "source_path": str(vod),
                    "source_index": 0,
                    "speed": 1.0,
                }
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
            "kill_banner_tier": tier_out,
            "kill_banner": label_out or meta.get("kill_banner"),
            "kill_banner_source": source_out,
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


def _presend_visual_ok(
    vis: dict,
    report: dict,
    row: dict,
) -> tuple[bool, str]:
    """Allow verified kill-banner clips when only the opening frame looks like menu/HUD."""
    if vis.get("visual_pass"):
        return True, ""
    fail = str(vis.get("fail_reason") or "")
    if not fail:
        return False, "visual:fail"
    banner_ok = str(report.get("kill_banner") or "").startswith(("banner_ok", "source_banner_ok"))
    tier = row.get("kill_banner_tier")
    try:
        tier_i = int(tier) if tier is not None else 0
    except (TypeError, ValueError):
        tier_i = 0
    if row.get("kill_banner") and tier_i <= 0:
        tier_i = 2
    if os.environ.get("MLBB_VOD_AUDIT_SEND", "0") == "1" and tier_i >= 2 and row.get("kill_banner"):
        return True, "audit_visual_bypass"
    min_tier = 2
    try:
        from mlbb_kill_banner import _min_tier

        min_tier = _min_tier()
    except Exception:
        pass
    if not banner_ok or tier_i < min_tier:
        return False, f"visual:{fail}"
    if os.environ.get("MLBB_VOD_PRESEND_SKIP_VISUAL_ON_BANNER", "1") != "1":
        return False, f"visual:{fail}"
    allowed_fails = {"menu_overlay", "hud_missing", "no_fight_in_frame"}
    reasons = [part.split(":", 1)[-1] for part in fail.split(",") if part]
    if not reasons or not all(r in allowed_fails for r in reasons):
        return False, f"visual:{fail}"
    cut_motion = float(report.get("cut_motion") or 0)
    peak_motion = float(report.get("peak_motion") or 0)
    cut_mini = float(report.get("cut_mini_delta") or 0)
    if max(cut_motion, peak_motion) < _presend_min_motion() * 0.85:
        return False, f"visual:{fail}"
    if cut_mini < _presend_min_minimap_delta() * 0.75:
        return False, f"visual:{fail}"
    return True, "visual_banner_bypass"


def _verified_discovery_banner(row: dict, min_tier: int) -> tuple[bool, str]:
    """Reuse an internally verified discovery hit; later POV/action gates still run."""
    strict = os.environ.get("MLBB_BANNER_SEND_STRICT", "1") == "1"
    source = str(row.get("kill_banner_source") or "")
    if strict and not source.endswith("_verified"):
        return False, ""
    if (
        not strict
        and os.environ.get("MLBB_VOD_PRESEND_FAST_BANNER", "1") != "1"
    ):
        return False, ""
    try:
        tier = int(row.get("kill_banner_tier") or 0)
    except (TypeError, ValueError):
        tier = 0
    banner = row.get("kill_banner")
    if not banner:
        return False, ""
    trust_min = int(min_tier)
    if os.environ.get("MLBB_VOD_DISCOVERY_TRUST_BASE_TIER", "1") == "1":
        try:
            from mlbb_kill_banner import _min_tier

            trust_min = _min_tier()
        except Exception:
            pass
    if tier < trust_min:
        return False, ""
    sec = float(row.get("banner_sec") or row.get("peak_start") or 0)
    return True, f"verified_discovery_banner:{banner}@{sec:.1f}s"


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
    audit_send = os.environ.get("MLBB_VOD_AUDIT_SEND", "0") == "1"
    if audit_send:
        return True, "audit_ok", report

    cut_start = float(row.get("start", 0))
    peak_start = float(row.get("peak_start", cut_start))
    dur = _segment_duration(row)
    audit_send = os.environ.get("MLBB_VOD_AUDIT_SEND", "0") == "1"

    if audit_send:
        ok, reason, freezes = True, "audit_freeze_skip", []
    else:
        ok, reason, freezes = _detect_render_freeze(rendered)
    report["freezes"] = freezes
    if not ok:
        return False, reason, report

    if os.environ.get("MLBB_VOD_KILL_BANNER", "1") == "1":
        presend_banner = os.environ.get("MLBB_VOD_BANNER_PRESEND", "1") == "1"
        if presend_banner:
            from mlbb_kill_banner import verify_banner_on_source, verify_rendered_clip, _min_tier

            banner_sec = float(row.get("banner_sec", peak_start)) if row.get("banner_sec") else peak_start
            if abs(banner_sec - peak_start) > 25.0:
                banner_sec = peak_start
            audit_send = os.environ.get("MLBB_VOD_AUDIT_SEND", "0") == "1"
            tier = row.get("kill_banner_tier")
            if tier is None and row.get("kill_banner"):
                tier = (row.get("kill_banner") or {}).get("tier")
            try:
                tier_i = int(tier) if tier is not None else 0
            except (TypeError, ValueError):
                tier_i = 0
            min_tier = _min_tier()
            title_min = int(os.environ.get("MLBB_VOD_TITLE_MIN_TIER", "0") or 0)
            if title_min > min_tier:
                min_tier = title_min
            banner_ok = False
            banner_reason = ""
            banner_ok, banner_reason = _verified_discovery_banner(row, min_tier)
            if audit_send and row.get("kill_banner"):
                banner_ok = True
                banner_reason = f"audit_trust:{row.get('kill_banner')}@{banner_sec:.1f}s"
            if not banner_ok:
                banner_ok, banner_reason = verify_banner_on_source(
                    vod,
                    banner_sec,
                    discovery_row=row,
                )
            if not banner_ok:
                banner_ok, banner_reason = verify_rendered_clip(
                    rendered,
                    banner_sec=banner_sec if row.get("banner_sec") else None,
                    clip_start=cut_start,
                )
            report["kill_banner"] = banner_reason
            if not banner_ok:
                return False, banner_reason, report
            if os.environ.get("MLBB_KILL_BANNER_REQUIRED", "1") == "1" and not audit_send:
                if tier_i < min_tier:
                    return (
                        False,
                        f"kill_banner_tier_low={tier_i}:need>={min_tier}",
                        report,
                    )
            if os.environ.get("MLBB_BANNER_POV_MATCH", "1") == "1" and not audit_send:
                from mlbb_banner_pov_match import banner_pov_hero_match_for_peak

                pov_ok, pov_reason, pov_sim = banner_pov_hero_match_for_peak(
                    vod,
                    peak_start,
                    banner_sec=float(row.get("banner_sec")) if row.get("banner_sec") else None,
                )
                report["pov_hero_sim"] = round(pov_sim, 4)
                if not pov_ok:
                    try:
                        tier_i = int(row.get("kill_banner_tier") or 0)
                    except (TypeError, ValueError):
                        tier_i = 0
                    pov_floor = float(os.environ.get("MLBB_BANNER_POV_MIN_SIM", "0.30"))
                    if tier_i >= 5 and pov_sim >= pov_floor * 0.45:
                        report["pov_soft_pass"] = True
                    else:
                        return False, pov_reason, report

    crop = _vod_crop_box(vod, cut_start, dur)
    report["crop"] = crop

    from gameplay_gate import segment_looks_like_rank_promo
    from mlbb_fight_segment import banner_in_vod_tail, clip_in_vod_tail

    banner_sec = float(row.get("banner_sec", peak_start)) if row.get("banner_sec") else peak_start
    if banner_in_vod_tail(vod, banner_sec):
        return False, f"vod_tail_banner@{banner_sec:.1f}s", report
    if clip_in_vod_tail(vod, cut_start, dur):
        return False, f"vod_tail_clip@{cut_start:.1f}s", report
    if segment_looks_like_rank_promo(vod, cut_start, dur, crop_box=crop):
        return False, "rank_promo_or_menu", report

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

    uniform_ok, uniform_reason = segment_uniform_gameplay_ok(
        vod, cut_start, dur, crop_box=crop, profile=PROFILE
    )
    report["uniform_reason"] = uniform_reason
    if not uniform_ok:
        return False, uniform_reason, report

    vis = extract_and_check_segment(vod, cut_start, dur, PROFILE, crop_box=crop)
    report["visual_pass"] = vis.get("visual_pass")
    report["visual_fail"] = vis.get("fail_reason", "")
    vis_ok, vis_reason = _presend_visual_ok(vis, report, row)
    if not vis_ok:
        return False, vis_reason, report
    if vis_reason == "visual_banner_bypass":
        report["visual_pass"] = True
        report["visual_fail"] = vis.get("fail_reason", "")

    rend_motion, rend_mini, rend_skill, _ = score_segment_combat(
        rendered, 0.0, min(dur, _ffprobe_duration(rendered)), sample_frames=6
    )
    report["render_motion"] = round(rend_motion, 4)
    if rend_motion < _presend_min_motion() * 0.75:
        return False, f"render_idle_motion={rend_motion:.4f}", report

    from mlbb_fight_segment import clip_action_sustain_ok

    tail_ok, tail_reason = clip_action_sustain_ok(vod, cut_start, dur, crop_box=crop)
    report["tail_action"] = tail_reason
    if not tail_ok:
        return False, tail_reason, report

    try:
        from mlbb_feedback_gate_tune import feedback_reject_row

        reject, reject_reason = feedback_reject_row(row)
        if reject:
            return False, reject_reason, report
    except Exception:
        pass

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
    return segment_gap_sec("mlbb", soften_level=level)


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
    """Keep best clip per fight — no time overlap between highlight windows."""

    def _rank_key(r: dict) -> tuple[float, float, float]:
        try:
            from mlbb_feedback_gate_tune import feedback_rank_key

            return feedback_rank_key(r)
        except Exception:
            metrics = r.get("highlight_metrics") or {}
            clip_score = float(metrics.get("clip_score") or r.get("clip_score") or 0.0)
            return (clip_score, float(r.get("score", 0)), float(r.get("hook_score", 0)), 0.0)

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
        taken.append((start, end))
        chosen.append(row)
    chosen.sort(key=lambda r: r["start"])
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
    deadline: float | None = None,
) -> tuple[list[dict], list[dict]]:
    from mlbb_vod_adaptive_gate import peak_near_skipped
    from vod_analysis_cache import cache_key_hash
    from vod_scan_state import minimal_pool_from_entry, pool_cache_valid

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
            if deadline is not None and time.time() >= deadline:
                log.warning(
                    "skip highlight scan — vod deadline reached vod=%s",
                    vod.name,
                )
                return [], pool or []
            from mlbb_fight_segment import clear_analysis_cache

            clear_analysis_cache()
            pool = discover_strict_candidates(vod, PROFILE, sig, set())
            if entry is not None:
                entry["last_analysis_cache_key"] = cache_key_hash(vod)
    skip_peaks = skip_peaks or set()
    labeled_set = set(labeled.keys()) if isinstance(labeled, dict) else set(labeled)
    min_gap = _segment_gap_sec()
    reserved_intervals = _used_intervals_for_vod(vod, labeled_set, sent)
    out: list[dict] = []
    min_peak = _vod_min_peak_sec(vod)
    gap = _interval_gap_sec()
    for clip in pool:
        peak = float(clip.get("start", 0))
        if peak_near_skipped(peak, skip_peaks):
            continue
        lead_clip = _normalize_clip(clip, vod)
        if lead_clip.get("banner_reject"):
            log.info(
                "skip peak=%.1f banner_reject=%s",
                peak,
                lead_clip.get("banner_reject"),
            )
            continue
        has_banner = bool(
            clip.get("kill_banner")
            or clip.get("anchor") == "kill_banner"
            or lead_clip.get("kill_banner")
            or lead_clip.get("anchor") == "kill_banner"
        )
        if peak < min_peak and not has_banner:
            log.info("skip peak=%.1f before min_peak=%.0f vod=%s", peak, min_peak, vod.name)
            continue
        start = float(lead_clip["start"])
        seg_dur = float(lead_clip.get("input_duration") or 0)
        from mlbb_fight_segment import ideal_clip_min_sec

        min_ideal = ideal_clip_min_sec()
        tier_i = int(lead_clip.get("kill_banner_tier") or 0)
        if tier_i >= 5:
            min_ideal = float(os.environ.get("MLBB_SAVAGE_CLIP_MIN_SEC", "8"))
        elif tier_i >= 4:
            min_ideal = float(os.environ.get("MLBB_MANIAC_CLIP_MIN_SEC", "12"))
        clip_tol = float(os.environ.get("MLBB_CLIP_DUR_TOLERANCE_SEC", "0.75"))
        if seg_dur + clip_tol < min_ideal:
            log.info("skip peak=%.1f short_ideal_clip dur=%.1f need=%.1f", peak, seg_dur, min_ideal)
            continue
        if seg_dur < float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7")):
            log.info("skip peak=%.1f short_banner_clip dur=%.1f", peak, seg_dur)
            continue
        if lead_clip.get("anchor") == "motion" and not lead_clip.get("kill_banner"):
            log.info("skip peak=%.1f motion_anchor_no_banner", peak)
            continue
        tier = lead_clip.get("kill_banner_tier")
        if tier is None and lead_clip.get("kill_banner"):
            tier = 0
        min_tier = 2 if os.environ.get("MLBB_KILL_BANNER_MIN_TIER", "double") == "double" else 1
        title_min = int(os.environ.get("MLBB_VOD_TITLE_MIN_TIER", "0") or 0)
        if title_min > min_tier:
            min_tier = title_min
        if os.environ.get("MLBB_KILL_BANNER_REQUIRED", "1") == "1":
            try:
                if int(tier or 0) < min_tier:
                    log.info(
                        "skip peak=%.1f banner_tier=%s need>=%s title_min=%s",
                        peak,
                        tier,
                        min_tier,
                        title_min,
                    )
                    continue
            except (TypeError, ValueError):
                log.info("skip peak=%.1f banner_tier_invalid", peak)
                continue
        banner_sec = float(lead_clip.get("banner_sec", lead_clip.get("peak_start", peak)))
        peak_ref = float(lead_clip.get("peak_start", peak))
        if abs(banner_sec - peak_ref) > float(os.environ.get("MLBB_BANNER_PEAK_MAX_DIST_SEC", "25")):
            log.info(
                "skip peak=%.1f banner_far_from_peak banner=%.1f peak_ref=%.1f",
                peak,
                banner_sec,
                peak_ref,
            )
            continue
        from mlbb_fight_segment import banner_in_vod_tail, clip_in_vod_tail

        if banner_in_vod_tail(vod, banner_sec):
            log.info(
                "skip peak=%.1f vod_tail_banner banner=%.1fs vod=%s",
                peak,
                banner_sec,
                vod.name,
            )
            continue
        if clip_in_vod_tail(vod, float(lead_clip.get("start", start)), seg_dur):
            log.info("skip peak=%.1f vod_tail_clip start=%.1f dur=%.1f", peak, start, seg_dur)
            continue
        if os.environ.get("MLBB_BANNER_POV_MATCH", "1") == "1":
            from mlbb_banner_pov_match import banner_pov_hero_match_for_peak

            pov_ok, pov_reason, pov_sim = banner_pov_hero_match_for_peak(
                vod,
                peak,
                banner_sec=float(lead_clip.get("banner_sec")) if lead_clip.get("banner_sec") else None,
            )
            if not pov_ok:
                if tier_i >= 5:
                    log.info(
                        "peak=%.1f pov_soft_pass savage banner sim=%.3f reason=%s",
                        peak,
                        pov_sim,
                        pov_reason,
                    )
                else:
                    log.info("skip peak=%.1f pov_fail=%s sim=%.3f", peak, pov_reason, pov_sim)
                    continue
        crop_probe = _vod_crop_box(vod, float(lead_clip.get("start", start)), seg_dur)
        from mlbb_fight_segment import clip_active_gameplay_ok

        title_need = int(os.environ.get("MLBB_VOD_TITLE_MIN_TIER", "0") or 0)
        # If the title promises a multi-kill moment, do not downgrade to a generic 1-kill fight.
        if title_need >= 2 and tier_i < title_need:
            log.info(
                "skip peak=%.1f title_multi_kill_mismatch tier=%s need=%s",
                peak,
                tier_i,
                title_need,
            )
            continue
        if tier_i >= 5 and title_need >= 5:
            active_ok, active_reason = True, "savage_title_trust"
        else:
            active_ok, active_reason = clip_active_gameplay_ok(
                vod, float(lead_clip.get("start", start)), seg_dur, crop_box=crop_probe
            )
        if not active_ok:
            log.info("skip peak=%.1f %s", peak, active_reason)
            continue
        end = start + float(lead_clip.get("input_duration") or _segment_duration({"start": start, "clip": lead_clip}))
        if _conflicts_any_interval(start, end, reserved_intervals, gap=gap):
            continue
        sid = segment_id(vod, start)
        if sid in labeled_set or sid in sent:
            continue
        hm = clip.get("highlight_metrics") or {}
        skip_revalidate = os.environ.get("MLBB_VOD_SKIP_REVALIDATE", "0") == "1"
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
            log.info("skip %s gate=%s", sid, reason)
            continue
        metrics = (metrics_rows[0] if metrics_rows else {}) or clip.get("highlight_metrics") or {}
        vis = visual_rows[0] if visual_rows else {}
        clip_score = float(
            metrics.get("clip_score") or clip.get("clip_score") or clip.get("score") or 0.0
        )
        min_clip = float(os.environ.get("MLBB_VOD_MIN_CLIP_SCORE", "0.05"))
        if clip_score < min_clip and os.environ.get("MLBB_VOD_OWNER_EXEMPLARS", "1") == "1":
            log.info("skip %s low_clip_score=%.3f min=%.3f", sid, clip_score, min_clip)
            continue
        candidate = {
            "segment_id": sid,
            "clip": lead_clip,
            "start": start,
            "peak_start": float(lead_clip.get("peak_start", peak)),
            "banner_sec": float(
                lead_clip.get("banner_sec", lead_clip.get("peak_start", peak))
            ),
            "fight_dur": float(lead_clip.get("input_duration", 0)),
            "kill_banner": lead_clip.get("kill_banner"),
            "kill_banner_tier": lead_clip.get("kill_banner_tier"),
            "kill_banner_source": lead_clip.get("kill_banner_source")
            or lead_clip.get("banner_source"),
            "score": float(clip.get("score") or metrics.get("viral_score") or 0),
            "hook_score": float(metrics.get("hook_score") or (clip.get("highlight_metrics") or {}).get("hook_score") or 0),
            "clip_score": clip_score,
            "highlight_metrics": metrics,
            "visual_pass": vis.get("visual_pass", True),
            "pass_reason": metrics.get("pass_reason") or metrics.get("gate_reason") or "",
            "gate_reason": reason,
        }
        try:
            from mlbb_feedback_gate_tune import feedback_reject_row

            reject, why = feedback_reject_row(candidate)
            if reject:
                log.info("skip %s %s", sid, why)
                continue
        except Exception:
            pass
        try:
            from mlbb_vod_quality_model import quality_gate

            quality_ok, quality_reason, quality_prob = quality_gate(candidate)
            candidate["quality_probability"] = quality_prob
            if not quality_ok:
                log.info("skip %s %s", sid, quality_reason)
                continue
        except Exception as exc:
            if os.environ.get("MLBB_VOD_QUALITY_MODEL_REQUIRED", "1") == "1":
                log.warning("skip %s quality_model_error=%s", sid, exc)
                continue
        out.append(candidate)
        if os.environ.get("MLBB_VOD_COLLECT_ONE", "0") == "1":
            log.info("collect_one: first validated segment %s — skip validating rest of pool", sid)
            break
    deduped = _dedupe_segments_by_gap(out, min_gap=min_gap, reserved_intervals=reserved_intervals)
    batch_cap = int(os.environ.get("MLBB_VOD_BATCH_MAX", "0"))
    if batch_cap > 0:
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
) -> tuple[int, int, int]:
    """Render and send segments — one Telegram video per clip (no montage merge)."""
    from mlbb_learning_first import can_send, daily_send_count, max_daily_sends

    send_one = os.environ.get("MLBB_VOD_SEND_ONE", "1") == "1"
    if send_one and len(to_send) > 1:
        to_send = to_send[:1]

    ok_batch, block_reason = can_send(1)
    if not ok_batch:
        log.warning("send batch blocked reason=%s", block_reason)
        send_message(
            token,
            chat_id,
            f"⛔ Отправка кусков приостановлена: {block_reason}\n"
            f"(LEARNING_FIRST gate — проверь MLBB_SEND_ENABLED=1 на VPS)",
        )
        return 0, 0, len(to_send)

    if os.environ.get("DAILY_GAME_CYCLE_ENABLED", "0") == "1":
        from daily_game_cycle import can_send_for_game

        cycle_game = (os.environ.get("VOD_SEGMENT_GAME") or "mlbb").strip().lower()
        ok_cycle, cycle_reason = can_send_for_game(cycle_game, 1)
        if not ok_cycle:
            log.warning("send batch blocked cycle=%s game=%s", cycle_reason, cycle_game)
            return 0, 0, len(to_send)

    cap_left = max_daily_sends() - daily_send_count()
    if cap_left <= 0:
        log.info("daily cap reached sent_today=%s cap=%s", daily_send_count(), max_daily_sends())
        return 0, 0, 0
    if len(to_send) > cap_left:
        log.info("daily cap trim batch %s -> %s", len(to_send), cap_left)
        to_send = to_send[:cap_left]

    if not send_one:
        seg_sec = int(float(os.environ.get("MLBB_VOD_SEGMENT_SEC", "15")))
        send_message(
            token,
            chat_id,
            f"MLBB VOD — {len(to_send)} кусков (~{seg_sec}с)\n"
            f"Стрим: {vod_youtube_id(vod)} ({vod.name})\n"
            f"👍 Ок / 👎 Не ок под каждым\n"
            f"Статистика: 👍{stats()['feedback_yes']} 👎{stats()['feedback_no']}",
        )
    SEGMENTS_ROOT = segments_root()
    SEGMENTS_ROOT.mkdir(parents=True, exist_ok=True)
    sent_ids: list[str] = []
    skipped: list[str] = []
    send_blocked = 0
    for row in to_send:
        sid = row["segment_id"]
        out = SEGMENTS_ROOT / f"seg_{sid}.mp4"
        force = os.environ.get("MLBB_FORCE_RERENDER", "0") == "1"
        if force or not out.exists() or out.stat().st_size < 500_000:
            if not render_single_segment(vod, row["clip"], out):
                skipped.append(f"{sid}:render_fail")
                continue
        presend_ok, presend_reason, presend_report = _validate_before_send(vod, row, out)
        if not presend_ok:
            log.warning("presend REJECT %s reason=%s report=%s", sid, presend_reason, presend_report)
            skipped.append(f"{sid}:{presend_reason}")
            continue
        seg_dur = _ffprobe_duration(out)
        peak = int(row.get("peak_start", row["start"]))
        report_line = _format_send_report(row, presend_report)
        clip_line = ""
        if row.get("clip_score") is not None:
            clip_line = f"learn={float(row['clip_score']):.3f} | "
        banner_line = ""
        if row.get("kill_banner"):
            banner_line = f"🎯 {str(row['kill_banner']).upper()} @ {peak}s\n"
        proof = str(presend_report.get("kill_banner") or "")
        if proof and ("owner_pos:" in proof or "ref_match:" in proof):
            banner_line += f"✓ {proof}\n"
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
    if (
        not sent_ids
        and skipped
        and os.environ.get("MLBB_VOD_PRESEND_REJECT_NOTIFY", "0") == "1"
    ):
        send_message(
            token,
            chat_id,
            f"⚠️ {vod_youtube_id(vod)}: {len(skipped)} кусков не прошли presend\n"
            + "\n".join(skipped[:6]),
        )
    if sent_ids:
        mark_feed_sent(sent_ids)
    return len(sent_ids), len(skipped), send_blocked


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
) -> int:
    """Drain all scorable segments from one VOD; kick off next download in parallel."""
    from mlbb_vod_adaptive_gate import (
        adaptive_env,
        apply_circuit_breaker,
        record_vod_outcome,
        should_notify_soften,
        soft_max_peak_tries,
        streak_from_state,
        telegram_exhaust_notice,
        telegram_soften_notice,
    )

    downloader.start_if_idle(registry)
    sent_total = 0
    max_per_vod = int(os.environ.get("MLBB_VOD_MAX_PER_VOD", "0"))
    sig = file_sha256(vod)
    sent = load_feed_sent()
    vid = vod_youtube_id(vod)
    try:
        from vod_pipeline_heartbeat import heartbeat

        heartbeat("vod_start", vod_id=vid, force=True)
    except Exception:
        pass
    _configure_banner_scan_policy(0)
    # Title-aware scan: savage in title → early start + require savage banner tier.
    try:
        from mlbb_vod_title import title_min_banner_tier, vod_title_blob

        title_blob = vod_title_blob(vod, entry)
        os.environ["MLBB_VOD_SCAN_TITLE"] = str((entry or {}).get("title") or "")
        title_tier = title_min_banner_tier(title_blob)
        if title_tier > 0:
            os.environ["MLBB_VOD_TITLE_MIN_TIER"] = str(title_tier)
            log.info(
                "title_gate vod=%s tier_need=%s blob=%s",
                vod.name,
                title_tier,
                title_blob[:80],
            )
        dense_scan = _configure_banner_scan_policy(
            title_tier,
            priority_rescan=bool(entry and entry.get("title_rescan_priority")),
        )
        if dense_scan:
            log.info(
                "dense_1hz vod=%s title_tier=%s rescan=%s",
                vod.name,
                title_tier,
                bool(entry and entry.get("title_rescan_priority")),
            )
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
    vod_deadline = time.time() + _vod_max_process_sec()
    labeled_set = set(labeled.keys()) if isinstance(labeled, dict) else set(labeled)
    lead = float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))

    # Owner-feedback gates from mined 👍/👎 patterns (hook/fight duration).
    if os.environ.get("MLBB_FEEDBACK_GATE", "1") == "1":
        try:
            from mlbb_feedback_gate_tune import apply_feedback_gates

            applied = apply_feedback_gates()
            if applied:
                log.info("feedback_gate applied=%s", applied)
        except Exception as exc:
            log.warning("feedback_gate skipped: %s", exc)

    # Quality mode: target owner 👎 share <= 20% by sending only high-confidence clips.
    # This can reduce volume but should improve precision quickly.
    quality_mode = os.environ.get("MLBB_VOD_QUALITY_MODE", "1") == "1"
    if quality_mode:
        try:
            from mlbb_vod_segment_store import stats as vod_stats

            st = vod_stats()
            yes = int(st.get("feedback_yes") or 0)
            no = int(st.get("feedback_no") or 0)
            rated = yes + no
            bad_share = (no / rated) if rated else 1.0
        except Exception:
            bad_share = 1.0
            rated = 0
        target = float(os.environ.get("MLBB_VOD_BAD_SHARE_TARGET", "0.20"))
        if bad_share > target:
            # Tighten score/hook only — MLBB HUD triggers false menu_overlay if visual is tightened.
            os.environ["MLBB_VOD_MIN_CLIP_SCORE"] = os.environ.get("MLBB_VOD_MIN_CLIP_SCORE", "0.10")
            os.environ["VIRAL_MLBB_HOOK_MIN"] = os.environ.get("VIRAL_MLBB_HOOK_MIN", "0.06")
            os.environ["MLBB_BANNER_MIN_HOOK"] = os.environ.get("MLBB_BANNER_MIN_HOOK", "0.07")
            os.environ["MLBB_BANNER_MIN_FIGHT_SEC"] = os.environ.get("MLBB_BANNER_MIN_FIGHT_SEC", "12")
            os.environ["MLBB_PRESEND_MIN_MOTION"] = os.environ.get("MLBB_PRESEND_MIN_MOTION", "0.015")
            os.environ["MLBB_SAVAGE_CLIP_MIN_SEC"] = os.environ.get("MLBB_SAVAGE_CLIP_MIN_SEC", "12")
            if os.environ.get("MLBB_VOD_QUALITY_MODE_VISUAL_TIGHTEN", "0") == "1":
                os.environ["SMART_UNIFORM_MIN_HUD_RATE"] = os.environ.get(
                    "SMART_UNIFORM_MIN_HUD_RATE", "0.70"
                )
                os.environ["MLBB_VOD_TAIL_MIN_HUD_RATE"] = os.environ.get(
                    "MLBB_VOD_TAIL_MIN_HUD_RATE", "0.55"
                )
                os.environ["VISUAL_MLBB_MENU_OVERLAY_MAX"] = os.environ.get(
                    "VISUAL_MLBB_MENU_OVERLAY_MAX", "0.78"
                )
            log.warning(
                "quality_mode tighten: rated=%s bad_share=%.2f target=%.2f",
                rated,
                bad_share,
                target,
            )

    clear_fast_seeds = None
    if os.environ.get("MLBB_VOD_FAST_PROBE", "1") == "1":
        from mlbb_vod_fast_scan import (
            apply_fast_probe_seeds,
            clear_fast_probe_seeds,
            vod_fast_combat_check,
        )

        clear_fast_seeds = clear_fast_probe_seeds
        ok_fast, fast_reason, seed_peaks = vod_fast_combat_check(vod, PROFILE)
        if not ok_fast:
            log.info("fast-probe weak vod=%s reason=%s — continue full scan", vod.name, fast_reason)
            seed_peaks = []
        elif seed_peaks:
            apply_fast_probe_seeds(seed_peaks)
        try:
            from vod_pipeline_heartbeat import heartbeat

            heartbeat(
                "fast_probe_done",
                vod_id=vid,
                candidates_out=len(seed_peaks),
                force=True,
            )
        except Exception:
            pass

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
            blocked_ids = labeled_set | sent
            index_segments = load_index().get("segments", [])
            used_peaks = used_peaks_for_vod("mlbb", vid, sent, index_segments)

            cached_blocked = False
            force_rescan = False
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
                    log.info("skip highlight rescan — cached peaks blocked vod=%s peaks=%s", vod.name, cached[:4])
                    record_vod_scan(entry, sent=0, pool_peaks=cached, blocked=True, pool=pool_cache)
                    cached_blocked = True

            while not cached_blocked:
                if time.time() >= vod_deadline:
                    log.warning(
                        "vod process deadline — stop vod=%s sent=%s max_min=%.0f",
                        vod.name,
                        sent_total,
                        _vod_max_process_sec() / 60.0,
                    )
                    break
                try:
                    from vod_pipeline_heartbeat import heartbeat

                    heartbeat(
                        "candidate_scan",
                        vod_id=vid,
                        candidates_in=len(pool_cache or []),
                        force=True,
                    )
                except Exception:
                    pass
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
                    deadline=vod_deadline,
                )
                pool_peaks = peaks_from_pool(pool_cache) if pool_cache is not None else []
                if not to_send:
                    blocked = False
                    min_peak = _vod_min_peak_sec(vod)
                    if pool_peaks and all(float(p) < min_peak for p in pool_peaks):
                        has_banner_peak = bool(
                            pool_cache
                            and any(
                                c.get("kill_banner") or c.get("anchor") == "kill_banner"
                                for c in pool_cache
                            )
                        )
                        if not has_banner_peak:
                            blocked = True
                            if entry is not None:
                                entry["reject_reason"] = "peaks_before_min_peak"
                            log.info(
                                "all pool peaks before min_peak=%.0f vod=%s peaks=%s",
                                min_peak,
                                vod.name,
                                pool_peaks[:6],
                            )
                    elif not pool_peaks:
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
                    if (
                        not blocked
                        and not force_rescan
                        and entry is not None
                        and pool_cache_valid(entry)
                    ):
                        log.warning(
                            "cached pool zero yield — invalidate and rescan vod=%s peaks=%s",
                            vod.name,
                            len(pool_peaks),
                        )
                        invalidate_pool_cache(entry)
                        pool_cache = None
                        force_rescan = True
                        continue
                    if entry is not None:
                        record_vod_scan(
                            entry,
                            sent=sent_total,
                            pool_peaks=pool_peaks,
                            blocked=blocked,
                            pool=pool_cache,
                        )
                    break
                n, preskip, sblock = _send_segment_batch(token, chat_id, vod, to_send, sig)
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
                            skip_peaks.add(round(float(row.get("peak_start", row["start"])), 1))
                            clip_peak = float(
                                (row.get("clip") or {}).get("start", row.get("start", 0))
                            )
                            skip_peaks.add(round(clip_peak, 1))
                            if row.get("banner_sec") is not None:
                                skip_peaks.add(round(float(row["banner_sec"]), 1))
                        peak_tries += 1
                        if peak_tries < max_peak_attempts:
                            log.warning(
                                "presend rejected peak — try next (%s/%s) vod=%s",
                                peak_tries,
                                max_peak_attempts,
                                vod.name,
                            )
                            continue
                        log.warning("batch presend rejected all — stop vod=%s", vod.name)
                        if entry is not None:
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

    if sent_total == 0 and not send_quota_blocked:
        if entry is not None:
            sessions = note_zero_send_session(entry)
            log.info("zero_send_sessions=%s vod=%s", sessions, vod.name)
            if sessions >= 2:
                entry.pop("title_rescan_priority", None)
        if entry and should_mark_vod_exhausted(entry):
            _mark_vod_exhausted(vod)
            entry["exhausted"] = True
            entry["id"] = vid
            entry.pop("title_rescan_priority", None)
            if not entry.get("reject_reason"):
                if not entry.get("last_pool_peaks"):
                    entry["reject_reason"] = "no_combat_peaks"
                elif entry.get("last_scan_blocked"):
                    entry["reject_reason"] = "all_peaks_blocked"
                else:
                    entry["reject_reason"] = "presend_rejected_all_peaks"
            _record_zero_yield_uploader(entry)
            log.info("exhausted vod=%s adaptive_streak=%s level=%s", vod.name, new_streak, active_level)
            if os.environ.get("MLBB_VOD_EXHAUST_NOTIFY", "1") == "1":
                send_message(
                    token,
                    chat_id,
                    telegram_exhaust_notice(vid, level=active_level, streak=new_streak),
                )
        else:
            log.info("zero send — keep vod=%s for retry (presend/soften) streak=%s", vod.name, new_streak)
    elif sent_total == 0 and send_quota_blocked:
        log.info("send quota blocked — keep vod=%s for next cycle", vod.name)
    else:
        log.info("sent=%s vod=%s (streak reset)", sent_total, vod.name)
        if entry is not None:
            entry["zero_send_sessions"] = 0
            entry.pop("title_rescan_priority", None)
        if active_level > 0 and os.environ.get("MLBB_VOD_ADAPTIVE_NOTIFY", "1") == "1":
            send_message(
                token,
                chat_id,
                f"✅ {sent_total} клип(ов) с мягких фильтров (L{active_level}) — возврат к strict после серии",
            )

    try:
        from mlbb_source_yield import record_vod_outcome as record_source_outcome

        record_source_outcome(
            entry,
            sent=sent_total,
            reject_reason=str((entry or {}).get("reject_reason") or ""),
        )
    except Exception as exc:
        log.warning("source yield record failed vod=%s: %s", vid, exc)
    try:
        from vod_pipeline_heartbeat import heartbeat

        heartbeat(
            "vod_done",
            vod_id=vid,
            candidates_out=sent_total,
            force=True,
        )
    except Exception:
        pass

    if entry:
        _sync_vod_entry_to_state(state, entry, vod)
    _save_state(state)
    return sent_total


def _bootstrap_shorts_exemplars_for_vod() -> dict:
    """Load 600+ 👍/👎 Shorts exemplars into CLIP cache for VOD peak scoring."""
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


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

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

    seg_sec = os.environ.get("MLBB_VOD_SEGMENT_SEC", "15")
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    if os.environ.get("MLBB_VOD_OWNER_EXEMPLARS", "1") == "1":
        os.environ["HIGHLIGHT_USE_OWNER_ANCHORS"] = "1"
        os.environ.setdefault("HIGHLIGHT_CLIP_DISABLED", "0")
    else:
        os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
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
        "MLBB_VOD_OWNER_EXEMPLARS",
        "HIGHLIGHT_USE_OWNER_ANCHORS",
    ):
        if key in env:
            os.environ[key] = str(env[key])
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


def _daily_cycle_mlbb_allowed() -> tuple[bool, str]:
    """True when daily cycle allows MLBB sends (re-check each VOD — avoid blocking PUBG/Standoff)."""
    if os.environ.get("DAILY_GAME_CYCLE_ENABLED", "0") != "1":
        return True, "cycle_disabled"
    from daily_game_cycle import active_game, can_send_for_game, reset_if_new_day

    reset_if_new_day()
    active = active_game()
    if active != "mlbb":
        return False, f"active_game={active}"
    ok_mlbb, why = can_send_for_game("mlbb", 1)
    if not ok_mlbb:
        return False, why
    return True, "ok"


def _run_feed(env: dict[str, str], token: str, chat_id: str) -> int:
    ok_start, why_start = _daily_cycle_mlbb_allowed()
    if not ok_start:
        log.info("daily cycle mlbb idle: %s", why_start)
        return 0

    from mlbb_vod_adaptive_gate import apply_circuit_breaker, streak_circuit_max

    state_cb = _load_state()
    if apply_circuit_breaker(state_cb):
        for row in state_cb.get("vods") or []:
            invalidate_pool_cache(row)
        _save_state(state_cb)
        log.warning("circuit breaker: reset zero streak (max=%s)", streak_circuit_max())

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

    registry = _ensure_registry(env)
    if _auto_exhaust_oversized(registry):
        state = _load_state()
        state["vods"] = registry
        _save_state(state)
    downloader = VodPipelineDownloader(env)
    total_sent = 0
    vods_done = 0
    notified_download = False

    # After repeated zero-yield VODs / long silence, automatically unlock throughput.
    from mlbb_vod_adaptive_gate import streak_from_state

    zero_send_streak = 0
    adaptive_streak = streak_from_state(state_cb)
    relaxed = False
    orig_quality_mode = os.environ.get("MLBB_VOD_QUALITY_MODE", "1")
    orig_min_clip = os.environ.get("MLBB_VOD_MIN_CLIP_SCORE", "0.06")
    orig_banner_min_hook = os.environ.get("MLBB_BANNER_MIN_HOOK", "0.05")
    orig_feedback = os.environ.get("MLBB_FEEDBACK_GATE", "1")
    orig_disable_soften = os.environ.get("MLBB_VOD_DISABLE_SOFTEN", "0")

    while time.time() < deadline and vods_done < max_vods:
        ok_cycle, cycle_reason = _daily_cycle_mlbb_allowed()
        if not ok_cycle:
            log.info("daily cycle yield after %s vods: %s", vods_done, cycle_reason)
            break

        # Apply quality relaxation before trying the next VOD.
        adaptive_streak = max(adaptive_streak, streak_from_state(_load_state()))
        relax_overrides = _mlbb_relax_overrides(
            zero_send_streak, adaptive_streak=adaptive_streak
        )
        if relax_overrides:
            if not relaxed:
                log.warning(
                    "throughput unlock after zeros=%s adaptive=%s silence=%.0fs",
                    zero_send_streak,
                    adaptive_streak,
                    _last_send_age_sec(),
                )
                relaxed = True
            os.environ.update(relax_overrides)
        elif relaxed:
            # Restore original defaults once we have a successful send.
            os.environ["MLBB_VOD_QUALITY_MODE"] = orig_quality_mode
            os.environ["MLBB_VOD_MIN_CLIP_SCORE"] = orig_min_clip
            os.environ["MLBB_BANNER_MIN_HOOK"] = orig_banner_min_hook
            os.environ["MLBB_FEEDBACK_GATE"] = orig_feedback
            os.environ["MLBB_VOD_DISABLE_SOFTEN"] = orig_disable_soften
            relaxed = False

        vod, entry = _resolve_next_vod(
            env,
            registry,
            downloader,
            auto_download=auto_download,
            token=token,
            chat_id=chat_id,
            notify=not notified_download,
        )
        if vod is None:
            if not notified_download and auto_download:
                send_message(token, chat_id, "⚠️ Не нашёл новый MLBB стрим. Повторю на следующем запуске.")
            break
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
        )
        total_sent += n
        if n > 0:
            zero_send_streak = 0
            adaptive_streak = 0
            if relaxed:
                os.environ["MLBB_VOD_QUALITY_MODE"] = orig_quality_mode
                os.environ["MLBB_VOD_MIN_CLIP_SCORE"] = orig_min_clip
                os.environ["MLBB_BANNER_MIN_HOOK"] = orig_banner_min_hook
                os.environ["MLBB_FEEDBACK_GATE"] = orig_feedback
                os.environ["MLBB_VOD_DISABLE_SOFTEN"] = orig_disable_soften
                relaxed = False
        else:
            zero_send_streak += 1
            adaptive_streak += 1
        vods_done += 1
        registry[:] = _ensure_registry(env)

    print(f"pipeline done sent={total_sent} vods={vods_done}")
    return 0 if total_sent > 0 or vods_done > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
