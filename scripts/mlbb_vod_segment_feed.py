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
    pool_peaks_fully_blocked,
    record_vod_scan,
    should_mark_vod_exhausted,
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


def _vod_max_sec() -> float:
    return float(os.environ.get("MLBB_VOD_MAX_SEC", "1200"))


def _vod_target_dur_sec() -> float:
    return float(os.environ.get("MLBB_VOD_TARGET_DUR_SEC", "780"))


def _vod_skip_long_sec() -> float:
    return float(os.environ.get("MLBB_VOD_SKIP_LONG_SEC", str(_vod_max_sec())))


def _vod_min_peak_sec(vod: Path | None = None) -> float:
    """Skip laning/spawn — fights usually after ~5–7 min on full VODs."""
    base = float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "420"))
    if vod is None:
        return base
    dur = _ffprobe_duration(vod)
    if dur <= 240:
        return min(base, 45.0)
    if dur <= 480:
        return min(base, 120.0)
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
    ranked: list[tuple[int, int, float, float, float, dict]] = []
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
        rich = _vod_richness_rank(row)
        ranked.append((rich, 1 if scanned else 0, scanned, abs(dur - target), dur, row))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    pick = ranked[0][5]
    dur = ranked[0][4]
    log.info(
        "pick vod id=%s dur_min=%.0f target_min=%.0f rich=%s scanned=%s pool=%s",
        pick.get("id", ""),
        dur / 60,
        target / 60,
        ranked[0][0],
        bool(ranked[0][1]),
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
    state = _load_state()
    return {str(u).casefold() for u in state.get("zero_yield_uploaders", []) if str(u).strip()}


def _record_zero_yield_uploader(meta: dict | None) -> None:
    if not meta:
        return
    from youtube_mlbb_vod_prefs import normalize_uploader

    uploader = normalize_uploader(meta)
    if not uploader:
        return
    state = _load_state()
    blocked = {str(u).casefold() for u in state.get("zero_yield_uploaders", [])}
    if uploader in blocked:
        return
    blocked.add(uploader)
    state["zero_yield_uploaders"] = sorted(blocked)[-200:]
    _save_state(state)
    log.info("zero-yield uploader blocked: %s", uploader)


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

    min_sec = _vod_min_sec()
    max_sec = _vod_max_sec()
    target = _vod_target_dur_sec()
    search_delay = float(os.environ.get("MLBB_VOD_SEARCH_DELAY", "5"))
    search_limit = int(os.environ.get("MLBB_VOD_SEARCH_LIMIT", "25"))
    blocked_uploaders = _zero_yield_uploaders()
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
        if LIVE_TITLE_RE.search(title) or LONG_VOD_TITLE_RE.search(title):
            skipped["live_or_long"] = skipped.get("live_or_long", 0) + 1
            continue
        if vid in used:
            continue
        if not passes_mlbb_game_title(title):
            skipped["not_mlbb"] = skipped.get("not_mlbb", 0) + 1
            continue
        if dur < min_sec or dur > max_sec:
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
    return float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))


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
        resolved = resolve_fight_bounds(vod, peak, file_dur)
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
        presend_banner = os.environ.get("MLBB_VOD_BANNER_PRESEND", "1") == "1"
        if presend_banner:
            from mlbb_kill_banner import verify_banner_on_source, verify_rendered_clip, _min_tier

            banner_sec = float(row.get("banner_sec", peak_start)) if row.get("banner_sec") else peak_start
            banner_ok, banner_reason = verify_banner_on_source(vod, banner_sec)
            if not banner_ok:
                banner_ok, banner_reason = verify_rendered_clip(
                    rendered,
                    banner_sec=banner_sec if row.get("banner_sec") else None,
                    clip_start=cut_start,
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
                min_tier = _min_tier()
                if tier_i < min_tier:
                    return (
                        False,
                        f"kill_banner_tier_low={tier_i}:need>={min_tier}",
                        report,
                    )

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

    uniform_ok, uniform_reason = segment_uniform_gameplay_ok(
        vod, cut_start, dur, crop_box=crop, profile=PROFILE
    )
    report["uniform_reason"] = uniform_reason
    if not uniform_ok:
        return False, uniform_reason, report

    vis = extract_and_check_segment(vod, cut_start, dur, PROFILE, crop_box=crop)
    report["visual_pass"] = vis.get("visual_pass")
    report["visual_fail"] = vis.get("fail_reason", "")
    if not vis.get("visual_pass"):
        # Confirmed kill-banner (OCR/ref) already proves this is a real fight moment —
        # HUD OCR at clip start is unreliable (zoom, death cam, banner flash).
        banner_ok_txt = str(report.get("kill_banner") or "")
        skip_vis = os.environ.get("MLBB_VOD_PRESEND_SKIP_VISUAL_ON_BANNER", "1") == "1"
        has_banner_meta = bool(
            row.get("kill_banner")
            or row.get("kill_banner_tier")
            or str(row.get("anchor") or "") == "kill_banner"
        )
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
        if peak < min_peak:
            continue
        lead_clip = _normalize_clip(clip, vod)
        if lead_clip.get("banner_reject"):
            log.info(
                "skip peak=%.1f banner_reject=%s",
                peak,
                lead_clip.get("banner_reject"),
            )
            continue
        start = float(lead_clip["start"])
        seg_dur = float(lead_clip.get("input_duration") or 0)
        if seg_dur < float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7")):
            log.info("skip peak=%.1f short_banner_clip dur=%.1f", peak, seg_dur)
            continue
        end = start + float(lead_clip.get("input_duration") or _segment_duration({"start": start, "clip": lead_clip}))
        if _conflicts_any_interval(start, end, reserved_intervals, gap=gap):
            continue
        sid = segment_id(vod, start)
        if sid in labeled_set or sid in sent:
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
            log.info("skip %s gate=%s", sid, reason)
            continue
        metrics = (metrics_rows[0] if metrics_rows else {}) or clip.get("highlight_metrics") or {}
        vis = visual_rows[0] if visual_rows else {}
        clip_score = float(metrics.get("clip_score") or 0.0)
        min_clip = float(os.environ.get("MLBB_VOD_MIN_CLIP_SCORE", "0.05"))
        if clip_score < min_clip and os.environ.get("MLBB_VOD_OWNER_EXEMPLARS", "1") == "1":
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
    deduped = _dedupe_segments_by_gap(out, min_gap=min_gap, reserved_intervals=reserved_intervals)
    batch_cap = int(os.environ.get("MLBB_VOD_BATCH_MAX", "0"))
    if montage_on:
        from mlbb_vod_montage import pick_montage_rows

        picked = pick_montage_rows(deduped)
        if picked:
            log.info(
                "montage pick vod=%s n=%s peaks=%s",
                vod.name,
                len(picked),
                [int(float(r.get("peak_start", r["start"]))) for r in picked],
            )
            deduped = picked
        elif batch_cap > 0:
            deduped = deduped[:batch_cap]
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
) -> tuple[int, int, int]:
    """Render and send segments — montage merge when MLBB_VOD_MONTAGE=1."""
    from mlbb_learning_first import can_send, daily_send_count, max_daily_sends
    from mlbb_vod_montage import montage_enabled, pick_montage_rows

    montage_on = montage_enabled() and len(to_send) >= 2
    if montage_on:
        picked = pick_montage_rows(to_send)
        if len(picked) >= 2:
            to_send = picked
        else:
            montage_on = False

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

    cap_left = max_daily_sends() - daily_send_count()
    if cap_left <= 0:
        log.info("daily cap reached sent_today=%s cap=%s", daily_send_count(), max_daily_sends())
        return 0, 0, 0

    if montage_on:
        return _send_montage_batch(token, chat_id, vod, to_send, sig)

    if not send_one:
        if len(to_send) > cap_left:
            log.info("daily cap trim batch %s -> %s", len(to_send), cap_left)
            to_send = to_send[:cap_left]
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
        send_message(
            token,
            chat_id,
            f"⚠️ {vod_youtube_id(vod)}: {len(skipped)} кусков не прошли presend\n"
            + "\n".join(skipped[:6]),
        )
    if sent_ids:
        mark_feed_sent(sent_ids)
    return len(sent_ids), len(skipped), send_blocked


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
            ok, reason, _rep = _validate_before_send(vod, row, part)
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
    state_pre = _load_state()
    streak_in = streak_from_state(state_pre)
    prev_level = int(state_pre.get("last_adaptive_level") or 0)
    active_level = 0
    pool_cache: list[dict] | None = None
    skip_peaks: set[float] = set()
    peak_tries = 0
    send_quota_blocked = False
    labeled_set = set(labeled.keys()) if isinstance(labeled, dict) else set(labeled)
    lead = float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))

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
            blocked_ids = labeled_set | sent
            index_segments = load_index().get("segments", [])
            used_peaks = used_peaks_for_vod("mlbb", vid, sent, index_segments)

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
                    log.info("skip highlight rescan — cached peaks blocked vod=%s peaks=%s", vod.name, cached[:4])
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
    _save_state(state)

    if sent_total == 0 and not send_quota_blocked:
        if entry:
            entry["zero_send_attempts"] = int(entry.get("zero_send_attempts") or 0) + 1
        if entry and should_mark_vod_exhausted(entry):
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
        "MLBB_VOD_MAX_ZERO_ATTEMPTS": "1",
        # Hard banner prefilter deletes whole VODs → endless ⚠️ spam.
        "MLBB_VOD_BANNER_HARD_PREFILTER": "0",
        "MLBB_VOD_BANNER_SKIP_ON_MISS": "0",
        # Reliable = clean bannered fights, not jumpy montage — but ALL banners.
        "MLBB_VOD_MONTAGE": "0",
        "MLBB_SKIP_MONTAGE": "1",
        "MLBB_VOD_SEND_ONE": "0",
        "MLBB_VOD_SEND_ALL_BANNERS": "1",
        "MLBB_VOD_MAX_PER_VOD": "5",
        "MLBB_VOD_PRESEND_SKIP_VISUAL_ON_BANNER": "1",
        "MLBB_KILL_BANNER_REQUIRED": "1",
        "MLBB_VOD_BANNER_PRESEND": "1",
        "MLBB_VOD_MOTION_ANCHOR_OK": "0",
        # Quality floor so OCR-blind soften cannot ship farming junk.
        "MLBB_RULE_COMBAT_MIN": "0.85",
        "HIGHLIGHT_MLBB_AUTO_CLIP_MIN": "0.12",
        "VIRAL_MLBB_CLIP_HOOK_MIN": "0.18",
        "MLBB_TEAMFIGHT_MIN_SCORE": "0.45",
        "MLBB_MOTION_PEAK_MAX": "4",
    }
    force = {
        "MLBB_VOD_ADAPTIVE_NOTIFY",
        "MLBB_VOD_EXHAUST_NOTIFY",
        "MLBB_VOD_DISCOVERY_MISS_NOTIFY",
        "MLBB_VOD_DOWNLOAD_NOTIFY",
        "MLBB_VOD_BANNER_HARD_PREFILTER",
        "MLBB_VOD_BANNER_SKIP_ON_MISS",
        "MLBB_VOD_MAX_ZERO_ATTEMPTS",
        "MLBB_VOD_MONTAGE",
        "MLBB_SKIP_MONTAGE",
        "MLBB_VOD_SEND_ONE",
        "MLBB_VOD_SEND_ALL_BANNERS",
        "MLBB_VOD_MAX_PER_VOD",
        "MLBB_VOD_PRESEND_SKIP_VISUAL_ON_BANNER",
        "MLBB_KILL_BANNER_REQUIRED",
        "MLBB_VOD_BANNER_PRESEND",
        "MLBB_VOD_MOTION_ANCHOR_OK",
        "MLBB_RULE_COMBAT_MIN",
        "HIGHLIGHT_MLBB_AUTO_CLIP_MIN",
        "VIRAL_MLBB_CLIP_HOOK_MIN",
        "MLBB_TEAMFIGHT_MIN_SCORE",
        "MLBB_MOTION_PEAK_MAX",
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

    registry = _ensure_registry(env)
    if _auto_exhaust_oversized(registry):
        state = _load_state()
        state["vods"] = registry
        _save_state(state)
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
            if (
                not notified_download
                and auto_download
                and os.environ.get("MLBB_VOD_DISCOVERY_MISS_NOTIFY", "0") == "1"
            ):
                send_message(token, chat_id, "⚠️ Не нашёл новый MLBB стрим. Повторю на следующем запуске.")
            else:
                log.info("discovery miss (notify muted)")
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
        vods_done += 1
        registry[:] = _ensure_registry(env)

    print(f"pipeline done sent={total_sent} vods={vods_done}")
    return 0 if total_sent > 0 or vods_done > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
