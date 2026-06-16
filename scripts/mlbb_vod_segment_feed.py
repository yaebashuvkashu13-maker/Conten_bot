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

from mlbb_vod_segment_store import (
    inline_keyboard_markup,
    labeled_ids,
    load_feed_sent,
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
from youtube_download import load_env
from youtube_watch_link import youtube_watch_url

ENV_PATH = Path("/root/.video_bot.env")
PROFILE = "mobile_legends"
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024
SEGMENT_SEC = float(os.environ.get("MLBB_VOD_SEGMENT_SEC", os.environ.get("HIGHLIGHT_WINDOW_SEC", "15")))
STATE_PATH = Path("/root/data/mlbb/vod_segment_state.json")
BLOCKED_VODS_PATH = Path(os.environ.get("MLBB_BLOCKED_VODS", "/root/data/mlbb/blocked_vods.json"))
VOD_LOCK_PATH = Path(os.environ.get("MLBB_VOD_FEED_LOCK", "/root/data/mlbb/vod_segment_feed.lock"))
YTDLP_LOCK_PATH = Path("/tmp/mlbb_vod_ytdlp.lock")
log = logging.getLogger("mlbb_vod_feed")

LONG_VOD_TITLE_RE = re.compile(
    r"\b\d+\s*h(?:our|rs?)?\b|\buncut\b|full\s+stream|live\s+stream|"
    r"час(?:ов)?\s+игр|полный\s+стрим",
    re.I,
)


def _vod_min_sec() -> float:
    return float(os.environ.get("MLBB_VOD_MIN_SEC", "300"))


def _vod_max_sec() -> float:
    return float(os.environ.get("MLBB_VOD_MAX_SEC", "1200"))


def _vod_target_dur_sec() -> float:
    return float(os.environ.get("MLBB_VOD_TARGET_DUR_SEC", "600"))


def _vod_skip_long_sec() -> float:
    return float(os.environ.get("MLBB_VOD_SKIP_LONG_SEC", str(_vod_max_sec())))


def _vod_min_peak_sec() -> float:
    """Skip laning/spawn — scaled down for 5–20 min matches."""
    return float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "120"))


def _blocked_vod_ids() -> set[str]:
    path = Path(os.environ.get("MLBB_BLOCKED_VODS", str(BLOCKED_VODS_PATH)))
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    return {str(v).replace("yt_", "") for v in data.get("vods", [])}


def _vod_unreliable(path: Path, dur: float | None = None) -> bool:
    """Long 60fps live archives and blocked ids produce frozen/bad clips."""
    length = dur if dur is not None else _ffprobe_duration(path)
    vid = vod_youtube_id(path)
    if vid in _blocked_vod_ids():
        return True
    max_live = float(os.environ.get("MLBB_FORCE_MAX_LIVE_VOD_SEC", "2700"))
    max_fps = float(os.environ.get("MLBB_FORCE_MAX_LIVE_VOD_FPS", "55"))
    return length > max_live and _ffprobe_fps(path) >= max_fps


def _vod_length_ok(path: Path, dur: float | None = None) -> bool:
    length = dur if dur is not None else _ffprobe_duration(path)
    if _vod_unreliable(path, length):
        return False
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
    if not STATE_PATH.exists():
        return {"active_vod": "", "scanned_vods": [], "vods": [], "used_youtube_ids": []}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"active_vod": "", "scanned_vods": [], "vods": [], "used_youtube_ids": []}
    data.setdefault("vods", [])
    data.setdefault("used_youtube_ids", [])
    data.setdefault("scanned_vods", [])
    return data


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _registry_entry(path: Path, *, title: str = "", exhausted: bool = False) -> dict:
    vid = vod_youtube_id(path)
    return {
        "id": vid,
        "path": str(path),
        "title": title or path.name,
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
    """Drop long/unreliable streams from queue — short matches scan 5–10× faster."""
    limit = _vod_skip_long_sec()
    n = 0
    for row in registry:
        if row.get("exhausted"):
            continue
        path = Path(str(row.get("path", "")))
        if not path.exists():
            continue
        dur = _ffprobe_duration(path)
        if dur > limit or _vod_unreliable(path, dur):
            row["exhausted"] = True
            n += 1
            log.info(
                "auto-exhaust vod id=%s dur_min=%.0f unreliable=%s",
                row.get("id", path.name),
                dur / 60,
                _vod_unreliable(path, dur),
            )
    return n


def _pick_available_vod(registry: list[dict]) -> dict | None:
    target = _vod_target_dur_sec()
    ranked: list[tuple[float, float, dict]] = []
    for row in registry:
        if row.get("exhausted"):
            continue
        path = Path(str(row.get("path", "")))
        if not path.exists():
            continue
        dur = _ffprobe_duration(path)
        if not _vod_length_ok(path, dur):
            continue
        ranked.append((abs(dur - target), dur, row))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]))
    pick = ranked[0][2]
    log.info(
        "pick vod id=%s dur_min=%.0f target_min=%.0f",
        pick.get("id", ""),
        ranked[0][1] / 60,
        target / 60,
    )
    return pick


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


def _discover_mlbb_vod_candidates(env: dict[str, str], used: set[str], *, throttled: bool = False) -> list[dict]:
    from nightly_youtube_montage import discover_candidates

    min_sec = _vod_min_sec()
    max_sec = _vod_max_sec()
    target = _vod_target_dur_sec()
    search_delay = float(os.environ.get("MLBB_VOD_SEARCH_DELAY", "5"))
    search_limit = int(os.environ.get("MLBB_VOD_SEARCH_LIMIT", "25"))
    queries = [
        q.strip()
        for q in os.environ.get(
            "MLBB_VOD_SEARCH_QUERIES",
            "MLBB VOD ranked match 10 minutes gameplay,MLBB mythic ranked 15 minutes teamfight,"
            "Mobile Legends solo rank match gameplay highlights,MLBB savage double kill ranked",
        ).split(",")
        if q.strip()
    ]
    if throttled and len(queries) > 1:
        # One query per background pass — less YouTube pressure than full sweep.
        queries = queries[:1]

    raw: list[dict] = []
    for idx, query in enumerate(queries):
        if throttled and idx > 0:
            time.sleep(search_delay)
        batch = discover_candidates(
            env, queries=[query], min_sec=min_sec, max_sec=max_sec, search_limit=search_limit
        )
        raw.extend(batch)

    out: list[dict] = []
    seen: set[str] = set()
    for meta in raw:
        vid = str(meta.get("id") or "")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        title = str(meta.get("title") or "")
        dur = float(meta.get("duration") or 0)
        if LIVE_TITLE_RE.search(title) or LONG_VOD_TITLE_RE.search(title):
            continue
        if vid in used:
            continue
        if not MLBB_TITLE_RE.search(title):
            continue
        if dur < min_sec or dur > max_sec:
            continue
        out.append(meta)
    out.sort(key=lambda m: abs(float(m.get("duration") or 0) - target), reverse=False)
    return out


def _download_vod_ytdlp_throttled(url: str, env: dict[str, str]) -> Path:
    from youtube_download import subprocess_env_no_proxy, ytdlp_cmd, ytdlp_extra_args, youtube_format_for_url

    delay = float(os.environ.get("MLBB_VOD_DOWNLOAD_DELAY", "12"))
    if delay > 0:
        time.sleep(delay)
    INBOX.mkdir(parents=True, exist_ok=True)
    template = str(INBOX / "yt_%(id)s.%(ext)s")
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        "--no-playlist",
        "--restrict-filenames",
        "--merge-output-format",
        "mp4",
        "-f",
        youtube_format_for_url(url, env),
        "--sleep-requests",
        env.get("YTDLP_SLEEP_REQUESTS", "1.5"),
        "--sleep-interval",
        env.get("YTDLP_SLEEP_INTERVAL", "4"),
        "--max-sleep-interval",
        env.get("YTDLP_MAX_SLEEP_INTERVAL", "12"),
        *ytdlp_extra_args(env),
        "-o",
        template,
        url,
    ]
    subprocess.run(
        cmd,
        check=True,
        timeout=int(env.get("YOUTUBE_DOWNLOAD_TIMEOUT", "14400")),
        env=subprocess_env_no_proxy(env),
    )
    files = sorted(INBOX.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise RuntimeError(f"yt-dlp produced no mp4 for {url}")
    return files[0]


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
        path = _download_vod_ytdlp_throttled(str(pick.get("url") or f"https://www.youtube.com/watch?v={pick['id']}"), env)

    entry = _registry_entry(path, title=str(pick.get("title", ""))[:120])
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
            if state.get("pending_download", {}).get("status") == "downloading":
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


def send_video(token: str, chat_id: str, path: Path, caption: str, *, seg_id: str) -> bool:
    from mlbb_learning_first import can_send, record_send

    ok_send, reason = can_send(1)
    if not ok_send:
        log.warning("send blocked seg=%s reason=%s", seg_id, reason)
        return False
    if path.stat().st_size > TELEGRAM_MAX_BYTES:
        return False
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    cmd = [
        "curl",
        "-sS",
        "--noproxy",
        "*",
        "-m",
        "600",
        "-F",
        f"chat_id={chat_id}",
        "-F",
        "supports_streaming=true",
        "-F",
        f"caption={caption[:900]}",
        "-F",
        f"reply_markup={json.dumps(inline_keyboard_markup(seg_id), ensure_ascii=False)}",
        "-F",
        f"video=@{path}",
        url,
    ]
    clean_env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    result = subprocess.run(cmd, capture_output=True, text=True, env=clean_env, timeout=620)
    try:
        sent = bool(json.loads(result.stdout).get("ok"))
        if sent:
            record_send(1)
        return sent
    except json.JSONDecodeError:
        return False


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
    if clip.get("preserve_duration"):
        return {
            **clip,
            "source_path": str(vod),
            "source_index": 0,
            "speed": float(clip.get("speed", 1.0)),
        }
    peak = float(clip.get("peak_start", clip.get("start", 0)))
    if os.environ.get("MLBB_VOD_VARIABLE_LENGTH", "1") == "1":
        from mlbb_fight_segment import detect_fight_bounds

        start, end, dur = detect_fight_bounds(vod, peak)
        return {
            **clip,
            "start": start,
            "peak_start": peak,
            "fight_end": end,
            "source_path": str(vod),
            "source_index": 0,
            "input_duration": dur,
            "output_duration": dur,
            "speed": 1.0,
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


def _crop_filter_prefix(vod: Path, start: float, dur: float) -> str:
    try:
        from video_orientation import is_portrait_video
    except ImportError:
        is_portrait_video = lambda _p: False  # type: ignore

    # Native portrait gameplay — keep full 9:16 frame.
    if is_portrait_video(vod):
        return ""
    # Landscape calibration: keep full 16:9 — skill buttons sit on the right edge.
    if os.environ.get("MLBB_VOD_FULL_FRAME", "1") == "1":
        return ""
    from smart_video_editor import detect_game_viewport_crop

    crop = detect_game_viewport_crop(vod, start, dur)
    if not crop or len(crop) != 4:
        return ""
    x, y, w, h = crop
    if w < 64 or h < 64:
        return ""
    return f"crop={w}:{h}:{x}:{y},"


def _scale_pad_vf(crop_prefix: str, source: Path, *, trailing: str) -> str:
    try:
        from video_orientation import target_render_size

        tw, th = target_render_size(source)
    except ImportError:
        from smart_video_editor import TARGET_HEIGHT, TARGET_WIDTH

        tw, th = int(TARGET_WIDTH), int(TARGET_HEIGHT)
    return (
        f"{crop_prefix}"
        f"scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black,"
        f"{trailing}"
    )


def _needs_chunk_render(vod: Path) -> bool:
    if os.environ.get("MLBB_VOD_CHUNK_RENDER", "1") != "1":
        return False
    if _ffprobe_fps(vod) >= 55.0:
        return True
    if vod.stat().st_size > 900_000_000:
        return True
    return _ffprobe_duration(vod) > 2700


def _extract_vod_chunk(vod: Path, rough_seek: float, chunk_dur: float, chunk_path: Path) -> bool:
    """Stage 1: decode short window to CFR chunk — hybrid seek avoids 60fps freeze."""
    from smart_video_editor import OUTPUT_FPS

    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    coarse_pad = float(os.environ.get("MLBB_CHUNK_COARSE_PAD", "10"))
    coarse = max(0.0, rough_seek - coarse_pad)
    fine = max(0.0, rough_seek - coarse)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-hwaccel",
        "none",
        "-ss",
        f"{coarse:.3f}",
        "-i",
        str(vod),
        "-ss",
        f"{fine:.3f}",
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
    return [
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


def _render_from_chunk(
    chunk_path: Path,
    *,
    source: Path,
    trim_start: float,
    dur: float,
    crop_prefix: str,
    out_path: Path,
    has_audio: bool,
) -> bool:
    """Stage 2: accurate -ss on small CFR chunk — no freeze."""
    from smart_video_editor import OUTPUT_FPS, run_command

    vf = _scale_pad_vf(
        crop_prefix,
        source,
        trailing=f"fps={OUTPUT_FPS},setpts=PTS-STARTPTS,format=yuv420p",
    )
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
    cmd.extend(["-t", f"{dur:.3f}"])
    cmd.extend(_vod_encode_args())
    cmd.append(str(out_path))
    run_command(cmd)
    return out_path.exists() and out_path.stat().st_size > 100_000


def _use_simple_render(clip: dict, dur: float) -> bool:
    if os.environ.get("MLBB_VOD_SIMPLE_RENDER", "0") == "1":
        return True
    if clip.get("preserve_duration"):
        return True
    return dur >= float(os.environ.get("MLBB_VOD_SIMPLE_RENDER_MIN_SEC", "18"))


def _render_simple_segment(
    vod: Path,
    *,
    start: float,
    dur: float,
    crop_prefix: str,
    out_path: Path,
    has_audio: bool,
) -> bool:
    """Single accurate -ss/-t — avoids double-seek buffer truncation on 20–30s clips."""
    from smart_video_editor import OUTPUT_FPS, run_command

    vf = _scale_pad_vf(crop_prefix, vod, trailing=f"fps={OUTPUT_FPS},format=yuv420p")
    os.environ.setdefault("SMART_OUTPUT_PRESET", "fast")
    cmd = [
        "ffmpeg",
        "-y",
        "-hwaccel",
        "none",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{dur:.3f}",
        "-i",
        str(vod),
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
    from smart_video_editor import OUTPUT_FPS, ffprobe_has_audio, run_command

    clip = _normalize_clip(clip, vod)
    start = float(clip["start"])
    dur = float(clip["input_duration"])
    pre_roll = _seek_preroll_sec(vod, start)
    rough_seek = max(0.0, start - pre_roll)
    trim_start = start - rough_seek
    crop_prefix = _crop_filter_prefix(vod, start, dur)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    has_audio = ffprobe_has_audio(vod)

    if _use_simple_render(clip, dur):
        return _render_simple_segment(
            vod,
            start=start,
            dur=dur,
            crop_prefix=crop_prefix,
            out_path=out_path,
            has_audio=has_audio,
        )

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
                    source=vod,
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
        + _scale_pad_vf(crop_prefix, vod, trailing=f"fps={OUTPUT_FPS},format=yuv420p")
    )
    os.environ.setdefault("SMART_OUTPUT_PRESET", "fast")
    coarse_pad = float(os.environ.get("MLBB_CHUNK_COARSE_PAD", "10"))
    coarse = max(0.0, rough_seek - coarse_pad)
    fine = max(0.0, rough_seek - coarse)
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
        f"{coarse:.3f}",
        "-i",
        str(vod),
        "-ss",
        f"{fine:.3f}",
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
    cmd.extend(["-t", f"{dur:.3f}"])
    cmd.extend(_vod_encode_args())
    cmd.append(str(out_path))
    run_command(cmd)
    return out_path.exists() and out_path.stat().st_size > 100_000


def _presend_freeze_min_dur() -> float:
    return float(os.environ.get("MLBB_PRESEND_FREEZE_MIN_DUR", "1.2"))


def _presend_freeze_max_start() -> float:
    return float(os.environ.get("MLBB_PRESEND_FREEZE_MAX_START", "3.0"))


def _presend_min_motion() -> float:
    return float(os.environ.get("MLBB_PRESEND_MIN_MOTION", "0.014"))


def _presend_min_minimap_delta() -> float:
    return float(os.environ.get("MLBB_PRESEND_MIN_MINIMAP_DELTA", "0.010"))


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
        detect_game_viewport_crop,
        score_segment_combat,
        segment_looks_like_draft_or_queue,
        segment_uniform_gameplay_ok,
    )
    from visual_action_check import extract_and_check_segment

    report: dict = {"segment_id": row.get("segment_id", "")}
    cut_start = float(row.get("start", 0))
    peak_start = float(row.get("peak_start", cut_start))
    clip = row.get("clip") or {}
    dur = float(
        row.get("fight_dur")
        or clip.get("input_duration")
        or clip.get("output_duration")
        or os.environ.get("MLBB_VOD_SEGMENT_SEC", "15")
    )

    ok, reason, freezes = _detect_render_freeze(rendered)
    report["freezes"] = freezes
    if not ok:
        return False, reason, report

    crop = detect_game_viewport_crop(vod, cut_start, dur)
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
    lines = [
        f"score={row['score']:.4f} hook={row['hook_score']:.3f}",
        f"gate={check.get('pass_reason', row.get('gate_reason', ''))}",
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
    default = max(45.0, SEGMENT_SEC * 3.0)
    return float(os.environ.get("MLBB_VOD_SEGMENT_GAP_SEC", os.environ.get("HIGHLIGHT_MIN_GAP_SEC", str(default))))


def _used_starts_for_vod(vod: Path, labeled: set[str], sent: set[str]) -> list[float]:
    vid = vod_youtube_id(vod)
    stem = vod.stem
    starts: list[float] = []
    for sid in labeled | sent:
        tail = sid.rsplit("_", 1)[-1]
        try:
            start = float(tail)
        except ValueError:
            continue
        if sid.startswith(f"{vid}_"):
            starts.append(start)
            continue
        # legacy ids from old yt_-prefix parser (yt_tp0aAJ22_622)
        if len(vid) >= 8 and vid[:8] in sid:
            starts.append(start)
            continue
        if stem in sid or stem.removeprefix("yt_") in sid:
            starts.append(start)
    return starts


def _dedupe_segments_by_gap(rows: list[dict], *, min_gap: float, reserved_starts: list[float]) -> list[dict]:
    """Keep best-scoring clip per fight — no 614/622/624 duplicates from one teamfight."""
    ranked = sorted(rows, key=lambda r: (r["score"], r["hook_score"]), reverse=True)
    taken = list(reserved_starts)
    chosen: list[dict] = []
    for row in ranked:
        start = float(row["start"])
        if any(abs(start - s) < min_gap for s in taken):
            continue
        taken.append(start)
        chosen.append(row)
    chosen.sort(key=lambda r: r["start"])
    return chosen


def _collect_kill_first_segments(
    vod: Path, sig: str, labeled: dict, sent: set, probe_limit: int
) -> list[dict]:
    """Fast path: kill UI peaks → variable fight bounds (7–22s)."""
    from mlbb_fight_segment import detect_fight_bounds
    from mlbb_kill_ui import passes_mlbb_kill_gate, result_is_multikill, scan_vod_kill_peaks

    labeled_set = set(labeled.keys()) if isinstance(labeled, dict) else set(labeled)
    min_gap = _segment_gap_sec()
    reserved = _used_starts_for_vod(vod, labeled_set, sent)
    scan_limit = max(probe_limit * 3, int(os.environ.get("MLBB_KILL_SCAN_LIMIT", "48")))
    peaks = scan_vod_kill_peaks(vod, limit=scan_limit)
    out: list[dict] = []
    min_peak = _vod_min_peak_sec()
    for peak_row in peaks:
        peak_t = float(peak_row.get("start_sec", 0))
        if peak_t < min_peak:
            continue
        if os.environ.get("MLBB_REQUIRE_MULTIKILL", "0") == "1" and not result_is_multikill(peak_row):
            continue
        start, end, dur = detect_fight_bounds(vod, peak_t)
        if any(abs(start - s) < min_gap for s in reserved):
            continue
        sid = segment_id(vod, start)
        if sid in labeled_set or sid in sent:
            continue
        ok, gate_reason, gate = passes_mlbb_kill_gate(vod, start, dur)
        if not ok:
            log.info("kill-first skip %s gate=%s", sid, gate_reason)
            continue
        score = float(peak_row.get("score", getattr(gate, "score", 0)))
        clip = {
            "source_path": str(vod),
            "source_index": 0,
            "game_name": "MLBB",
            "start": start,
            "peak_start": peak_t,
            "fight_end": end,
            "input_duration": dur,
            "output_duration": dur,
            "speed": 1.0,
            "score": score,
            "highlight_metrics": {"pass_reason": peak_row.get("reason", "kill_ui")},
            "gate_reason": f"kill_ui:{gate_reason}",
        }
        out.append(
            {
                "segment_id": sid,
                "clip": clip,
                "start": start,
                "peak_start": peak_t,
                "fight_dur": dur,
                "score": score,
                "hook_score": 0.0,
                "visual_pass": True,
                "pass_reason": peak_row.get("reason", "kill_ui"),
                "clip_score": score,
                "gate_reason": gate_reason,
            }
        )
    deduped = _dedupe_segments_by_gap(out, min_gap=min_gap, reserved_starts=reserved)
    batch_cap = int(os.environ.get("MLBB_VOD_BATCH_MAX", "30"))
    if batch_cap > 0:
        deduped = deduped[:batch_cap]
    log.info("kill-first vod=%s peaks=%s unique=%s", vod.name, len(peaks), len(deduped))
    return deduped


def _collect_scan_segments(vod: Path, sig: str, labeled: dict, sent: set, probe_limit: int) -> list[dict]:
    if os.environ.get("MLBB_VOD_KILL_FIRST", "1") == "1":
        return _collect_kill_first_segments(vod, sig, labeled, sent, probe_limit)

    labeled_set = set(labeled.keys()) if isinstance(labeled, dict) else set(labeled)
    min_gap = _segment_gap_sec()
    reserved = _used_starts_for_vod(vod, labeled_set, sent)
    pool = discover_strict_candidates(vod, PROFILE, sig, set())
    out: list[dict] = []
    min_peak = _vod_min_peak_sec()
    for clip in pool:
        peak = float(clip.get("start", 0))
        if peak < min_peak:
            continue
        lead_clip = _normalize_clip(clip, vod)
        start = float(lead_clip["start"])
        if any(abs(start - s) < min_gap for s in reserved):
            continue
        sid = segment_id(vod, start)
        if sid in labeled_set or sid in sent:
            continue
        ok, reason, _, metrics_rows, visual_rows = validate_clips_before_preview(
            vod, PROFILE, [lead_clip]
        )
        if not ok:
            log.info("skip %s gate=%s", sid, reason)
            continue
        metrics = (metrics_rows[0] if metrics_rows else {}) or clip.get("highlight_metrics") or {}
        vis = visual_rows[0] if visual_rows else {}
        out.append(
            {
                "segment_id": sid,
                "clip": lead_clip,
                "start": start,
                "peak_start": float(lead_clip.get("peak_start", peak)),
                "fight_dur": float(lead_clip.get("input_duration", 0)),
                "score": float(clip.get("score") or metrics.get("viral_score") or 0),
                "hook_score": float(metrics.get("hook_score") or (clip.get("highlight_metrics") or {}).get("hook_score") or 0),
                "visual_pass": vis.get("visual_pass", True),
                "pass_reason": metrics.get("pass_reason") or metrics.get("gate_reason") or "",
                "clip_score": float(metrics.get("clip_score") or 0),
                "gate_reason": reason,
            }
        )
    deduped = _dedupe_segments_by_gap(out, min_gap=min_gap, reserved_starts=reserved)
    batch_cap = int(os.environ.get("MLBB_VOD_BATCH_MAX", "30"))
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
    return deduped


def _send_segment_batch(
    token: str,
    chat_id: str,
    vod: Path,
    to_send: list[dict],
    sig: str,
) -> int:
    from mlbb_learning_first import daily_send_count, max_daily_sends

    cap_left = max_daily_sends() - daily_send_count()
    if cap_left <= 0:
        log.info("daily cap reached sent_today=%s cap=%s", daily_send_count(), max_daily_sends())
        return 0
    if len(to_send) > cap_left:
        log.info("daily cap trim batch %s -> %s", len(to_send), cap_left)
        to_send = to_send[:cap_left]
    seg_sec = int(float(os.environ.get("MLBB_VOD_SEGMENT_SEC", "15")))
    avg_dur = int(
        round(
            sum(float(r.get("fight_dur", r.get("clip", {}).get("output_duration", seg_sec))) for r in to_send)
            / max(1, len(to_send))
        )
    )
    send_message(
        token,
        chat_id,
        f"MLBB VOD — {len(to_send)} хайлайтов (~{avg_dur}с, двойное убийство+)\n"
        f"Стрим: {vod_youtube_id(vod)} ({vod.name})\n"
        f"👍 Ок / 👎 Не ок под каждым\n"
        f"Статистика: 👍{stats()['feedback_yes']} 👎{stats()['feedback_no']}",
    )
    SEGMENTS_ROOT = segments_root()
    SEGMENTS_ROOT.mkdir(parents=True, exist_ok=True)
    sent_ids: list[str] = []
    skipped: list[str] = []
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
            out.unlink(missing_ok=True)
            skipped.append(f"{sid}:{presend_reason}")
            continue
        seg_dur = _ffprobe_duration(out)
        report_line = _format_send_report(row, presend_report)
        watch = youtube_watch_url(vod_youtube_id(vod), float(row["start"]))
        caption = (
            f"MLBB кусок #{sid}\n"
            f"{watch}\n"
            f"кусок {seg_dur:.0f}с\n"
            f"{report_line}\n"
            f"✓ presend\n"
            f"👍 Ок / 👎 Не ок"
        )
        if not send_video(token, chat_id, out, caption, seg_id=sid):
            send_message(token, chat_id, f"{caption}\n(файл >20MB — не отправился)")
        upsert_segment(
            {
                "segment_id": sid,
                "path": str(out),
                "vod": str(vod),
                "vod_id": vod_youtube_id(vod),
                "start": row["start"],
                "score": row["score"],
                "hook_score": row["hook_score"],
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
    mark_feed_sent(sent_ids)
    return len(sent_ids)


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
    downloader.start_if_idle(registry)
    sent_total = 0
    sig = file_sha256(vod)
    sent = load_feed_sent()
    max_clips = int(os.environ.get("MLBB_VOD_MAX_CLIPS_PER_RUN", "15"))

    while True:
        if max_clips > 0 and sent_total >= max_clips:
            log.info("vod clip cap reached sent=%s cap=%s vod=%s", sent_total, max_clips, vod.name)
            break
        to_send = _collect_scan_segments(vod, sig, labeled, sent, probe_limit)
        if not to_send:
            break
        if max_clips > 0 and sent_total + len(to_send) > max_clips:
            to_send = to_send[: max_clips - sent_total]
        n = _send_segment_batch(token, chat_id, vod, to_send, sig)
        if n == 0:
            log.warning("batch had candidates but none sent — stop vod=%s", vod.name)
            break
        sent_total += n
        sent = load_feed_sent()
        downloader.start_if_idle(registry)

    state = _load_state()
    state["active_vod"] = vod.name
    scanned = set(state.get("scanned_vods", []))
    scanned.add(vod.name)
    state["scanned_vods"] = sorted(scanned)
    _save_state(state)

    if sent_total == 0:
        _mark_vod_exhausted(vod)
        if entry:
            entry["exhausted"] = True
            entry["id"] = vod_youtube_id(vod)
        log.info("exhausted vod=%s", vod.name)
    else:
        log.info("sent=%s vod=%s", sent_total, vod.name)
    return sent_total


def _acquire_vod_lock() -> object | None:
    VOD_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = VOD_LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        print("skip: another vod_segment_feed is running", flush=True)
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    lock_handle = _acquire_vod_lock()
    if lock_handle is None:
        return 0

    try:
        return _run_vod_pipeline()
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_handle.close()


def _run_vod_pipeline() -> int:

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
    os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    os.environ.setdefault("STRICT_PROBE_LIMIT", os.environ.get("MLBB_VOD_PROBE_LIMIT", "50"))
    os.environ.setdefault("OWNER_PREVIEW_REQUIRED", "0")
    os.environ["LOGO_FILE"] = "/nonexistent/mlbb_calibration_no_logo.png"
    os.environ.setdefault("MLBB_VOD_SEGMENT_SEC", seg_sec)
    os.environ.setdefault("HIGHLIGHT_WINDOW_SEC", seg_sec)

    for key, val in strict_peak_env(PROFILE).items():
        os.environ[key] = val

    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1

    labeled = labeled_ids()
    probe_limit = int(os.environ.get("MLBB_VOD_PROBE_LIMIT", "12"))
    auto_download = os.environ.get("MLBB_VOD_AUTO_DOWNLOAD", "1") == "1"
    max_min = float(os.environ.get("MLBB_VOD_PIPELINE_MAX_MIN", "360"))
    deadline = time.time() + max_min * 60
    max_vods = int(os.environ.get("MLBB_VOD_PIPELINE_MAX_VODS", "4"))

    registry = _ensure_registry(env)
    if _auto_exhaust_oversized(registry):
        state = _load_state()
        state["vods"] = registry
        _save_state(state)
    downloader = VodPipelineDownloader(env)
    total_sent = 0
    vods_done = 0
    notified_download = False
    max_total_clips = int(os.environ.get("MLBB_VOD_MAX_CLIPS_PER_RUN", "15"))

    while time.time() < deadline and vods_done < max_vods:
        if max_total_clips > 0 and total_sent >= max_total_clips:
            log.info("pipeline clip cap reached sent=%s cap=%s", total_sent, max_total_clips)
            break
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
        vods_done += 1
        if max_total_clips > 0 and total_sent >= max_total_clips:
            log.info("pipeline clip cap after vod=%s sent=%s", vod.name, total_sent)
            break
        registry[:] = _ensure_registry(env)

    print(f"pipeline done sent={total_sent} vods={vods_done}")
    return 0 if total_sent > 0 or vods_done > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
