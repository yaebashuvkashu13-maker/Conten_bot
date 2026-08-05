#!/usr/bin/env python3
"""MLBB in-game kill-streak banner detection (Triple Kill, Maniac, Savage, …)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import logging

log = logging.getLogger("mlbb_kill_banner")


def _analysis_series(analysis: dict[str, Any], key: str) -> list[float]:
    """Safe array extraction — analysis values may be list or numpy ndarray."""
    raw = analysis.get(key)
    if raw is None:
        return []
    try:
        import numpy as np

        if isinstance(raw, np.ndarray):
            return raw.astype(np.float32).tolist()
    except Exception:
        pass
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    return []

# tier: 1=weak … 5=best. Default min tier: double (2) and above.
TIER_LABELS = {
    5: "savage",
    4: "maniac",
    3: "triple",
    2: "double",
    1: "single",
}

_ENEMY_STREAK_RE = re.compile(
    r"(?:"
    r"enemy\s+(?:triple|double|maniac|savage|legendary|quadra|penta|killing|rampage|"
    r"unstoppable|dominating|god|wiped|ace)"
    r"|вражеск.{0,16}(?:тройн|трипл|маньяк|саваж|легенд)"
    r"|противник.{0,16}(?:тройн|трипл|маньяк|саваж)"
    r")",
    re.I,
)

_STREAK_PATTERNS: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"savage|саваж", re.I), 5, "savage"),
    (re.compile(r"legendary|легендар", re.I), 5, "legendary"),
    (re.compile(r"maniac|маньяк", re.I), 4, "maniac"),
    (re.compile(r"ruthless|беспощад|безжалост", re.I), 4, "ruthless"),
    (re.compile(r"triple\s*kill|тройн.{0,12}убий", re.I), 3, "triple"),
    (re.compile(r"ultra\s*kill", re.I), 3, "triple"),
    (re.compile(r"double\s*kill|двойн.{0,12}убий|ou?ble\s*kill|d0uble|2\s*x\s*kill", re.I), 2, "double"),
    (re.compile(r"\bkill\b|убийств", re.I), 1, "single"),
]


@dataclass(frozen=True)
class KillBannerHit:
    sec: float
    tier: int
    label: str
    text: str
    source: str = "ocr"


def _min_tier() -> int:
    raw = (os.environ.get("MLBB_KILL_BANNER_MIN_TIER") or "double").strip().lower()
    if raw.isdigit():
        return max(1, int(raw))
    return {"single": 1, "double": 2, "triple": 3, "maniac": 4, "savage": 5}.get(raw, 2)


def _banner_required() -> bool:
    return os.environ.get("MLBB_KILL_BANNER_REQUIRED", "0") == "1"


def _motion_anchor_ok() -> bool:
    """Motion fight bounds are acceptable without a verified kill-banner anchor."""
    if os.environ.get("MLBB_VOD_MOTION_ANCHOR_OK", "0") == "1":
        return True
    if not _banner_required():
        return True
    if os.environ.get("MLBB_VOD_BANNER_PRESEND", "0") != "1":
        return True
    return False


def _scan_step() -> float:
    return float(os.environ.get("MLBB_KILL_BANNER_SCAN_STEP", "0.35"))


def _color_min_score() -> float:
    return float(os.environ.get("MLBB_KILL_BANNER_COLOR_MIN", "0.045"))


def classify_banner_text(text: str) -> KillBannerHit | None:
    blob = " ".join(str(text or "").split())
    if not blob:
        return None
    if _ENEMY_STREAK_RE.search(blob):
        return None
    best_tier = 0
    best_label = ""
    for pat, tier, label in _STREAK_PATTERNS:
        if pat.search(blob) and tier > best_tier:
            best_tier = tier
            best_label = label
    if best_tier <= 0:
        return None
    return KillBannerHit(sec=0.0, tier=best_tier, label=best_label, text=blob[:120])


def _announce_color_score(frame) -> float:
    """Score MLBB kill-announce flash in the top-center HUD zone.

    Real banners are bluish/cyan (HSV H≈95–115 on owner/wiki samples), not gold.
    White text glow is secondary evidence.
    """
    import cv2
    import numpy as np

    small = cv2.resize(frame, (320, 180))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    h, w = small.shape[:2]
    zone = hsv[int(h * 0.02) : int(h * 0.30), int(w * 0.15) : int(w * 0.85)]
    if zone.size == 0:
        return 0.0
    # OpenCV H is 0–179. Owner kill photos median ~H103/S121/V161; wiki classic ~H107.
    cyan = cv2.inRange(zone, np.array([75, 55, 95]), np.array([130, 255, 255]))
    white = cv2.inRange(zone, np.array([0, 0, 210]), np.array([180, 50, 255]))
    n = float(cyan.size)
    cyan_ratio = float(np.count_nonzero(cyan)) / n
    white_ratio = float(np.count_nonzero(white)) / n
    # Cyan dominates; white text glow is secondary so plain bright UI does not dominate.
    ratio = cyan_ratio + 0.45 * white_ratio
    return min(1.0, ratio * 11.0)


def _ocr_banner_zones(frame, *, deep: bool = False) -> str:
    import cv2

    try:
        import pytesseract
    except ImportError:
        return ""

    small = cv2.resize(frame, (480, 270))
    h, w = small.shape[:2]
    zones = [
        small[int(h * 0.02) : int(h * 0.28), int(w * 0.10) : int(w * 0.90)],
        small[int(h * 0.04) : int(h * 0.32), int(w * 0.18) : int(w * 0.82)],
    ]
    if deep:
        zones.append(small[int(h * 0.08) : int(h * 0.38), int(w * 0.02) : int(w * 0.38)])
    texts: list[str] = []
    psms = (7, 8, 6) if deep else (7,)
    for zone in zones:
        if zone.size == 0:
            continue
        gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
        variants = [cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]]
        if deep:
            variants.append(cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1])
        for variant in variants:
            for psm in psms:
                try:
                    text = pytesseract.image_to_string(
                        variant,
                        config=f"--psm {psm} -l eng+rus",
                    )
                except Exception:
                    continue
                text = " ".join(text.split())
                if text:
                    texts.append(text)
                    if not deep and classify_banner_text(text) is not None:
                        return " ".join(texts)
    return " ".join(texts)


def _ocr_center_banner(frame) -> str:
    return _ocr_banner_zones(frame)


def _read_frame(vod: Path, sec: float):
    from gameplay_gate import _read_frame_at

    return _read_frame_at(vod, sec)


def _ffmpeg_sample_frames(vod: Path, t0: float, t1: float, sample_count: int) -> list[tuple[float, object]]:
    import numpy as np

    duration = max(0.25, t1 - t0)
    fps = max(1.0, sample_count / duration)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-hwaccel",
        "none",
        "-ss",
        f"{max(0.0, t0):.3f}",
        "-i",
        str(vod),
        "-t",
        f"{duration:.3f}",
        "-vf",
        f"fps={fps:.3f},scale=480:270",
        "-frames:v",
        str(max(1, sample_count)),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False, timeout=45)
    if proc.returncode != 0 or not proc.stdout:
        return []
    frame_bytes = 480 * 270 * 3
    raw = proc.stdout
    frames: list[tuple[float, object]] = []
    for idx in range(sample_count):
        offset = idx * frame_bytes
        chunk = raw[offset : offset + frame_bytes]
        if len(chunk) < frame_bytes:
            break
        frame = np.frombuffer(chunk, dtype=np.uint8).reshape((270, 480, 3)).copy()
        sec = t0 + (idx + 0.5) * duration / max(sample_count, 1)
        frames.append((sec, frame))
    return frames


def _sample_frames(vod: Path, t0: float, t1: float) -> list[tuple[float, object]]:
    step = max(0.25, _scan_step())
    span = max(0.0, t1 - t0)
    sample_count = max(3, min(36, int(span / step) + 1))
    frames = _ffmpeg_sample_frames(vod, t0, t1, sample_count)
    if frames:
        return frames
    out: list[tuple[float, object]] = []
    t = max(0.0, t0)
    end = max(t, t1)
    while t <= end:
        frame = _read_frame(vod, t)
        if frame is not None:
            out.append((t, frame))
        t += step
    return out


def _ocr_ok() -> bool:
    """OCR is blind on most VODs — off by default. Use wiki/owner refs instead."""
    return os.environ.get("MLBB_BANNER_OCR_OK", "0") == "1"


def _classify_frame(sec: float, frame, *, deep: bool = False) -> KillBannerHit | None:
    """
    Useful banner detect order (OCR is NOT the ship gate):
    1) Visual reference match (wiki skins + owner kill photos) — primary
    2) OCR text only when MLBB_BANNER_OCR_OK=1 (default off — Tess is blind here)
    3) Cyan flash is a probe tip only — never invent double/triple from color alone
    """
    color = _announce_color_score(frame)
    color_gate = _color_min_score() * (0.55 if deep else 0.75)

    # Visual bank first — humans recognize banner shape/skin, not Tesseract noise.
    if color >= color_gate or deep:
        try:
            from mlbb_banner_ref_match import classify_banner_reference

            ref_hit = classify_banner_reference(sec, frame)
            if ref_hit is not None:
                return ref_hit
        except Exception:
            pass

    if _ocr_ok() and (color >= color_gate or deep):
        classified = classify_banner_text(_ocr_banner_zones(frame, deep=deep))
        if classified is not None:
            return KillBannerHit(
                sec=round(sec, 2),
                tier=classified.tier,
                label=classified.label,
                text=classified.text,
                source="ocr",
            )

    if color >= _color_min_score():
        if _ocr_ok():
            deep_text = _ocr_banner_zones(frame, deep=True)
            if _ENEMY_STREAK_RE.search(deep_text):
                return None
            classified = classify_banner_text(deep_text)
            if classified is not None:
                return KillBannerHit(
                    sec=round(sec, 2),
                    tier=classified.tier,
                    label=classified.label,
                    text=classified.text,
                    source="ocr",
                )
        # Cyan/blue pixels alone are NOT a kill streak. Inventing double/triple
        # from color_flash ships jog clips with zero kills — never do that by default.
        if _color_as_streak_allowed():
            tier = 3 if color >= _color_min_score() * 1.6 else 2
            return KillBannerHit(
                sec=round(sec, 2),
                tier=tier,
                label="triple" if tier >= 3 else "double",
                text=f"color_flash={color:.3f}",
                source="flash",
            )
        return None
    return None


def _color_only_allowed() -> bool:
    """Legacy: allow color-only hits in verify_rendered_clip. Default off."""
    return os.environ.get("MLBB_KILL_BANNER_COLOR_ONLY", "0") == "1"


def _color_as_streak_allowed() -> bool:
    """Opt-in only: treat cyan flash as fake double/triple. Default OFF."""
    return os.environ.get("MLBB_BANNER_COLOR_AS_STREAK", "0") == "1"


def _visual_banner_ok() -> bool:
    """Accept wiki/owner visual ref match as a real banner hit (primary ship gate)."""
    return os.environ.get("MLBB_BANNER_VISUAL_OK", "1") == "1"


def _hit_qualifies(hit: KillBannerHit, *, min_tier: int) -> bool:
    if hit.tier < min_tier:
        return False
    # Primary: visual ref (wiki + owner photos).
    if hit.source == "ref" and _visual_banner_ok():
        return True
    # OCR only when explicitly enabled — default off (blind Tess).
    if hit.source == "ocr":
        return _ocr_ok()
    # flash/color never ship as ≥double unless explicitly opted in.
    if hit.source in ("flash", "color"):
        return _color_as_streak_allowed() and _visual_banner_ok()
    return not _banner_required()


def _source_rank(source: str) -> int:
    """Lower is better. Ref beats OCR; flash/color last."""
    return {"ref": 0, "ocr": 1, "flash": 2, "color": 3}.get(str(source or ""), 9)

def _candidate_secs(
    frames: list[tuple[float, object]],
    *,
    focus_sec: float | None = None,
    max_ocr: int = 5,
) -> list[float]:
    if not frames:
        return []
    scored: list[tuple[float, float]] = []
    for sec, frame in frames:
        scored.append((sec, _announce_color_score(frame)))
    scored.sort(key=lambda row: row[1], reverse=True)
    picks: list[float] = []
    for sec, color in scored:
        if color < _color_min_score() * 0.65 and picks:
            break
        picks.append(sec)
        if len(picks) >= max_ocr:
            break
    if focus_sec is not None:
        nearest = min(frames, key=lambda row: abs(row[0] - focus_sec))[0]
        if nearest not in picks:
            picks.insert(0, nearest)
    return picks[: max_ocr + 1]


def scan_window(
    vod: Path,
    t0: float,
    t1: float,
    *,
    focus_sec: float | None = None,
    deep: bool = False,
    quick: bool = False,
) -> list[KillBannerHit]:
    """Scan [t0, t1] for kill-streak banners; cyan tip prefilter then ref (OCR optional)."""
    if quick:
        deep = False
        span = max(0.0, t1 - t0)
        sample_count = max(3, min(6, int(span / 0.5) + 1))
        frames = _ffmpeg_sample_frames(vod, t0, t1, sample_count)
        if not frames:
            frames = _sample_frames(vod, t0, t1)[:6]
        max_ocr = 2
    else:
        frames = _sample_frames(vod, t0, t1)
        max_ocr = 6 if deep else 4
    hits: list[KillBannerHit] = []
    frame_map = {sec: frame for sec, frame in frames}
    for sec in _candidate_secs(frames, focus_sec=focus_sec, max_ocr=max_ocr):
        frame = frame_map.get(sec)
        if frame is None:
            continue
        hit = _classify_frame(sec, frame, deep=deep)
        if hit is not None:
            hits.append(hit)
    if not hits and frames and not quick:
        for sec, frame in frames:
            hit = _classify_frame(sec, frame, deep=True)
            if hit is not None and hit.source in ("ref", "ocr"):
                hits.append(hit)
                break
    hits.sort(key=lambda h: (-h.tier, _source_rank(h.source), h.sec))
    return hits


def find_banner_near_peak(vod: Path, peak_sec: float, *, quick: bool = False) -> KillBannerHit | None:
    """Look for streak banner around motion peak (banner at/just after peak)."""
    if quick:
        before = float(os.environ.get("MLBB_KILL_BANNER_QUICK_BEFORE", "12"))
        after = float(os.environ.get("MLBB_KILL_BANNER_QUICK_AFTER", "8"))
        hits = scan_window(vod, peak_sec - before, peak_sec + after, focus_sec=peak_sec, quick=True)
    else:
        before = float(os.environ.get("MLBB_KILL_BANNER_SCAN_BEFORE", "24"))
        after = float(os.environ.get("MLBB_KILL_BANNER_SCAN_AFTER", "12"))
        hits = scan_window(vod, peak_sec - before, peak_sec + after, focus_sec=peak_sec, deep=True)
    if not hits:
        return None
    min_tier = _min_tier()
    # Prefer ref > OCR > flash/color, then higher tier, then closer to peak.
    ranked = sorted(
        hits,
        key=lambda h: (
            _source_rank(h.source),
            -h.tier,
            abs(h.sec - peak_sec),
        ),
    )
    for hit in ranked:
        if _hit_qualifies(hit, min_tier=min_tier):
            return hit
    return None


def _adaptive_banner_scan_start(vod: Path, duration: float) -> float:
    """Earliest sec to scan for banners — short VODs have fights before 5 min."""
    base = float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "300"))
    if duration <= 240:
        return 15.0
    if duration <= 480:
        return min(base, 90.0)
    return base


def _motion_hint_peaks(analysis: dict, *, limit: int, duration: float) -> list[float]:
    """Derive probe times from motion/audio when caller passes no hint_peaks."""
    import numpy as np

    win = float(analysis.get("window_seconds", 2.0) or 2.0)
    if duration <= 240:
        t0 = 15.0
    elif duration <= 480:
        t0 = min(float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "300")), 90.0)
    else:
        t0 = float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "300"))

    motion = np.asarray(_analysis_series(analysis, "center_motion"), dtype=np.float32)
    audio = np.asarray(_analysis_series(analysis, "audio"), dtype=np.float32)
    picked: list[float] = []
    if motion.size >= 4:
        combined = motion if audio.size != motion.size else motion * 0.55 + audio * 0.45
        for bi in np.argsort(combined)[::-1]:
            t = float(bi) * win + win * 0.5
            if t < t0 or t > duration - 4.0:
                continue
            if any(abs(t - p) < 8.0 for p in picked):
                continue
            picked.append(round(t, 2))
            if len(picked) >= limit:
                return picked

    t1 = max(t0 + 30.0, duration - 20.0)
    if t1 <= t0 + 1.0:
        return [round(duration * 0.5, 2)]
    step = (t1 - t0) / max(limit, 1)
    return [round(t0 + (i + 0.5) * step, 2) for i in range(limit)]


def _duration_grid_peaks(duration: float, *, limit: int) -> list[float]:
    """Mid-game probe grid — no full-VOD analyze required."""
    if duration <= 240:
        t0 = 20.0
    elif duration <= 480:
        t0 = min(float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "300")), 90.0)
    else:
        t0 = float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "120"))
        t0 = min(t0, 120.0)
    t1 = max(t0 + 40.0, duration - 25.0)
    if t1 <= t0 + 1.0:
        return [round(duration * 0.45, 2)]
    step = float(os.environ.get("MLBB_BANNER_FAST_STEP_SEC", "28"))
    peaks: list[float] = []
    t = t0
    while t < t1 and len(peaks) < limit * 3:
        peaks.append(round(t, 2))
        t += step
    # Prefer denser mid-game samples if grid is sparse.
    if len(peaks) < limit:
        mid = t0 + (t1 - t0) * 0.45
        for delta in (-90, -45, 0, 45, 90, 150):
            p = round(mid + delta, 2)
            if t0 <= p <= t1 and all(abs(p - x) > 12 for x in peaks):
                peaks.append(p)
            if len(peaks) >= limit:
                break
    return peaks[: max(limit, 1)]


def _color_tip_rank(vod: Path, peaks: list[float]) -> list[float]:
    """Cheap single-frame cyan tip sort — does NOT invent kill tiers."""
    from gameplay_gate import _read_frame_at

    scored: list[tuple[float, float]] = []
    for t in peaks:
        frame = _read_frame_at(vod, t)
        color = float(_announce_color_score(frame)) if frame is not None else 0.0
        scored.append((t, color))
    scored.sort(key=lambda row: (-row[1], row[0]))
    # Keep tips that look announce-ish first, but never drop all peaks.
    gate = _color_min_score() * 0.5
    tipped = [t for t, c in scored if c >= gate]
    if len(tipped) >= 3:
        return tipped
    return [t for t, _ in scored]


def discover_vod_kill_banners_fast(
    vod: Path,
    *,
    min_tier: int | None = None,
    hint_peaks: list[float] | None = None,
) -> list[KillBannerHit]:
    """
    Fast + correct discover: ffprobe grid → cyan tip rank → visual ref match.
    OCR only if MLBB_BANNER_OCR_OK=1. Never invents double from color alone.
    """
    if os.environ.get("MLBB_VOD_KILL_BANNER", "1") != "1":
        return []
    if os.environ.get("MLBB_VOD_BANNER_DISCOVER", "1") != "1":
        return []

    from smart_video_editor import ffprobe_duration

    duration = float(ffprobe_duration(vod) or 0.0)
    if duration < 20.0:
        return []
    need = min_tier if min_tier is not None else _min_tier()
    max_probes = max(4, int(os.environ.get("MLBB_BANNER_FAST_MAX_PROBES", "10")))
    max_sec = max(20.0, float(os.environ.get("MLBB_BANNER_FAST_MAX_SEC", "75")))
    ship_on_first = os.environ.get("MLBB_BANNER_FAST_SHIP_ON_FIRST", "1") == "1"
    deadline = time.monotonic() + max_sec
    hits: list[KillBannerHit] = []
    probes = 0

    peaks = [float(p) for p in (hint_peaks or []) if p is not None]
    if not peaks:
        peaks = _duration_grid_peaks(duration, limit=max_probes)
    peaks = _color_tip_rank(vod, peaks)[: max_probes * 2]

    log.info(
        "banner fast-discover %s: peaks=%s budget=%.0fs need_tier>=%s",
        vod.name,
        [round(p, 1) for p in peaks[:12]],
        max_sec,
        need,
    )

    for peak in peaks:
        if probes >= max_probes or time.monotonic() >= deadline:
            break
        probes += 1
        hit = find_banner_near_peak(vod, peak, quick=True)
        if hit is None:
            continue
        if not _hit_qualifies(hit, min_tier=need):
            continue
        # Safety: never accept flash/color here even if misconfigured.
        if hit.source in ("flash", "color") and not _color_as_streak_allowed():
            continue
        if hits and abs(hit.sec - hits[-1].sec) < 6.0:
            if hit.tier > hits[-1].tier or (
                hit.tier == hits[-1].tier
                and _source_rank(hit.source) < _source_rank(hits[-1].source)
            ):
                hits[-1] = hit
        else:
            hits.append(hit)
        log.info(
            "banner fast-hit %s: @%.1fs tier=%s src=%s label=%s probes=%s",
            vod.name,
            hit.sec,
            hit.tier,
            hit.source,
            hit.label,
            probes,
        )
        if ship_on_first and hits:
            break

    hits.sort(key=lambda h: (-h.tier, _source_rank(h.source), h.sec))
    log.info(
        "banner fast-discover %s: done probes=%s hits=%s elapsed=%.1fs",
        vod.name,
        probes,
        len(hits),
        max_sec - max(0.0, deadline - time.monotonic()),
    )
    return hits


def discover_vod_kill_banners(
    vod: Path,
    *,
    min_tier: int | None = None,
    hint_peaks: list[float] | None = None,
) -> list[KillBannerHit]:
    """
    Kill-banner discover. Default: fast path (no full analyze).
    Set MLBB_BANNER_FAST_DISCOVER=0 to force legacy motion-analyze discover.
    """
    if os.environ.get("MLBB_VOD_KILL_BANNER", "1") != "1":
        return []
    if os.environ.get("MLBB_VOD_BANNER_DISCOVER", "1") != "1":
        return []
    # Fast path first — correct (visual ref primary) and skips multi-minute analyze.
    if os.environ.get("MLBB_BANNER_FAST_DISCOVER", "1") == "1":
        fast_hits = discover_vod_kill_banners_fast(
            vod, min_tier=min_tier, hint_peaks=hint_peaks
        )
        if fast_hits:
            return fast_hits
        # Optional one deep retry on the best tip only when fast found nothing.
        if os.environ.get("MLBB_BANNER_FAST_DEEP_RETRY", "1") == "1" and hint_peaks:
            need = min_tier if min_tier is not None else _min_tier()
            for peak in list(hint_peaks)[:2]:
                hit = find_banner_near_peak(vod, float(peak), quick=False)
                if hit is not None and _hit_qualifies(hit, min_tier=need):
                    if hit.source in ("flash", "color") and not _color_as_streak_allowed():
                        continue
                    return [hit]
        return []

    import numpy as np

    from mlbb_fight_segment import _analysis_for

    analysis = _analysis_for(vod)
    duration = float(analysis.get("duration") or 0.0)
    if duration < 20.0:
        return []
    need = min_tier if min_tier is not None else _min_tier()
    max_probes = max(4, int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_MAX_PROBES", "16")))
    max_sec = max(30.0, float(os.environ.get("MLBB_KILL_BANNER_DISCOVER_MAX_SEC", "120")))
    deadline = time.monotonic() + max_sec
    hits: list[KillBannerHit] = []
    probes = 0

    def _merge_hit(hit: KillBannerHit) -> None:
        if not _hit_qualifies(hit, min_tier=need):
            return
        if hit.source in ("flash", "color") and not _color_as_streak_allowed():
            return
        if hits and hit.sec - hits[-1].sec < 6.0:
            if hit.tier > hits[-1].tier or (
                hit.tier == hits[-1].tier
                and _source_rank(hit.source) < _source_rank(hits[-1].source)
            ):
                hits[-1] = hit
        else:
            hits.append(hit)

    def _probe_at(t: float, *, deep: bool) -> bool:
        nonlocal probes
        if probes >= max_probes or time.monotonic() >= deadline:
            return False
        probes += 1
        for hit in scan_window(vod, t - 0.5, t + 2.5, focus_sec=t, deep=deep):
            _merge_hit(hit)
            if hits:
                return True
        return probes < max_probes and time.monotonic() < deadline

    # Phase 1: narrow scan around motion peaks (caller hints or auto-derived).
    peak_limit = max(4, int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_PEAK_HINTS", "6")))
    peaks = [float(p) for p in (hint_peaks or []) if p is not None]
    if not peaks:
        peaks = _motion_hint_peaks(analysis, limit=peak_limit, duration=duration)
        log.info(
            "banner discover %s: auto hint peaks=%s",
            vod.name,
            [round(p, 1) for p in peaks[:peak_limit]],
        )
    for peak in sorted(set(peaks))[:peak_limit]:
        if probes >= max_probes or time.monotonic() >= deadline:
            break
        probes += 1
        hit = find_banner_near_peak(vod, peak, quick=True)
        if hit:
            _merge_hit(hit)

    # Phase 2 (full VOD motion sweep) is opt-in — default off; it can stall for hours.
    # If peaks-only produced nothing, still do a short motion sweep so quota hunts
    # are not stuck with probes=0 / hits=0 after a long analyze_video.
    force_sparse = (
        not hits
        and probes > 0
        and os.environ.get("MLBB_VOD_BANNER_DISCOVER_FALLBACK_SPARSE", "1") == "1"
    )
    if os.environ.get("MLBB_VOD_BANNER_DISCOVER_FULL", "0") != "1" and not force_sparse:
        hits.sort(key=lambda h: h.sec)
        log.info(
            "banner discover %s: peaks-only probes=%s hits=%s",
            vod.name,
            probes,
            len(hits),
        )
        return hits
    if force_sparse:
        log.info(
            "banner discover %s: fallback sparse after empty peaks-only probes=%s",
            vod.name,
            probes,
        )

    # Phase 2: sparse motion-gated sweep for banners away from motion peaks.
    if probes < max_probes and time.monotonic() < deadline:
        win = float(analysis.get("window_seconds", 2.0))
        motion = np.asarray(_analysis_series(analysis, "center_motion"), dtype=np.float32)
        audio = np.asarray(_analysis_series(analysis, "audio"), dtype=np.float32)
        combined = motion if audio.size != motion.size else motion * 0.55 + audio * 0.45
        motion_thr = float(np.percentile(combined, 35)) if combined.size > 4 else 0.0
        step = float(os.environ.get("MLBB_KILL_BANNER_DISCOVER_STEP", "5.0"))
        t0 = _adaptive_banner_scan_start(vod, duration)
        t = t0
        while t < duration - 4.0 and probes < max_probes and time.monotonic() < deadline:
            bi = min(int(t / max(win, 0.5)), max(0, combined.size - 1))
            if combined.size > bi and float(combined[bi]) < motion_thr:
                t += step
                continue
            if probes % 5 == 0:
                log.info(
                    "banner discover %s: probe=%s/%s t=%.0fs hits=%s",
                    vod.name,
                    probes,
                    max_probes,
                    t,
                    len(hits),
                )
            if not _probe_at(t, deep=False):
                break
            t += step

    hits.sort(key=lambda h: h.sec)
    log.info(
        "banner discover %s: done probes=%s hits=%s elapsed=%.0fs",
        vod.name,
        probes,
        len(hits),
        max_sec - max(0.0, deadline - time.monotonic()),
    )
    return hits


def filter_peaks_with_ocr_banner(
    vod: Path,
    peaks: list[float],
    *,
    max_probe: int | None = None,
    known_banners: list[KillBannerHit] | None = None,
) -> list[float]:
    """Keep motion peaks that have a qualifying kill banner nearby (ref primary)."""
    if os.environ.get("MLBB_VOD_BANNER_PREFILTER", "1") != "1":
        return peaks
    limit = max_probe or int(os.environ.get("MLBB_VOD_BANNER_PREFILTER_PEAKS", "16"))
    need = _min_tier()
    before = float(os.environ.get("MLBB_KILL_BANNER_SCAN_BEFORE", "20"))
    after = float(os.environ.get("MLBB_KILL_BANNER_SCAN_AFTER", "10"))
    qualified = [
        h
        for h in (known_banners or [])
        if _hit_qualifies(h, min_tier=need)
    ]
    if qualified:
        kept: list[float] = []
        for peak in peaks[: max(1, limit)]:
            for hit in qualified:
                if (hit.sec - before) <= peak <= (hit.sec + after) or abs(hit.sec - peak) <= before + 5:
                    kept.append(peak)
                    break
        return kept
    kept: list[float] = []
    probe_cap = min(limit, int(os.environ.get("MLBB_VOD_BANNER_PREFILTER_OCR_PEAKS", "8")))
    for peak in peaks[: max(1, probe_cap)]:
        hit = find_banner_near_peak(vod, peak, quick=True)
        if hit and _hit_qualifies(hit, min_tier=need):
            kept.append(peak)
    return kept


def bounds_from_banner(
    banner_sec: float,
    file_dur: float,
    *,
    fight_start: float | None = None,
    fight_end: float | None = None,
) -> tuple[float, float, float]:
    """Clip bounds: fight sustain window anchored on banner, not fixed lead/post."""
    from mlbb_fight_segment import _fight_min_sec, _fight_max_sec, _fight_hard_max_sec, _lead_sec

    min_d = _fight_min_sec()
    max_d = _fight_max_sec()
    hard_max = _fight_hard_max_sec()
    lead = _lead_sec()

    if fight_start is not None and fight_end is not None and fight_end > fight_start:
        start = max(0.0, float(fight_start))
        end = min(float(file_dur), float(fight_end))
    else:
        start = max(0.0, float(banner_sec) - lead)
        tail = max(min_d * 0.5, (max_d - lead) * 0.55)
        end = min(float(file_dur), float(banner_sec) + tail)

    if float(banner_sec) < start:
        start = max(0.0, float(banner_sec) - lead)
    if float(banner_sec) > end:
        end = min(float(file_dur), float(banner_sec) + max(2.0, min_d * 0.4))

    dur = end - start
    if dur < min_d:
        end = min(file_dur, start + min_d)
        dur = end - start
    if dur > hard_max:
        end = start + hard_max
        dur = hard_max
    elif dur > max_d:
        end = start + max_d
        dur = max_d

    # Montage: banner should not sit in the last ~30% (post-fight running / idle tail).
    banner_rel = (float(banner_sec) - start) / max(dur, 1e-6)
    if dur >= 10.0 and banner_rel > 0.68:
        post = max(3.0, lead * 0.85)
        pre = max(min_d - post, min_d * 0.5)
        start = max(0.0, float(banner_sec) - pre)
        end = min(float(file_dur), float(banner_sec) + post)
        dur = end - start
        if dur < min_d:
            end = min(file_dur, start + min_d)
            dur = end - start

    return round(start, 2), round(end, 2), round(dur, 2)


def resolve_fight_bounds(
    vod: Path,
    peak_sec: float,
    file_dur: float,
) -> tuple[float, float, float, dict] | None:
    """
    Prefer kill-banner anchor (visual ref; OCR only if enabled) inside motion sustain.
    Falls back to motion when banner missing and motion_anchor / combat mode allows it.
    """
    from mlbb_combat_moment import moment_anchor_mode
    from mlbb_fight_segment import detect_fight_bounds

    fight_start, fight_end, fight_dur = detect_fight_bounds(vod, peak_sec)
    motion_meta = {
        "anchor": "motion",
        "banner_sec": peak_sec,
        "fight_start": fight_start,
        "fight_end": fight_end,
        "fight_dur": fight_dur,
    }

    if os.environ.get("MLBB_VOD_KILL_BANNER", "1") != "1":
        return fight_start, fight_end, fight_dur, motion_meta

    hit = find_banner_near_peak(vod, peak_sec, quick=True)
    if hit is None:
        hit = find_banner_near_peak(vod, peak_sec, quick=False)
    min_tier = _min_tier()
    mode = moment_anchor_mode()

    if hit is not None and _hit_qualifies(hit, min_tier=min_tier):
        start, end, dur = bounds_from_banner(
            hit.sec,
            file_dur,
            fight_start=fight_start,
            fight_end=fight_end,
        )
        return (
            start,
            end,
            dur,
            {
                "anchor": "kill_banner",
                "banner_sec": hit.sec,
                "kill_banner": hit.label,
                "kill_banner_tier": hit.tier,
                "banner_text": hit.text,
                "banner_source": hit.source,
                "fight_start": fight_start,
                "fight_end": fight_end,
                "fight_dur": fight_dur,
            },
        )

    # No banner: combat/motion modes keep the fight; strict banner mode rejects.
    if mode in ("combat", "motion") or _motion_anchor_ok():
        return fight_start, fight_end, fight_dur, motion_meta
    if not _banner_required():
        return fight_start, fight_end, fight_dur, motion_meta
    return None


def verify_banner_on_source(
    vod: Path,
    banner_sec: float,
    *,
    min_tier: int | None = None,
) -> tuple[bool, str]:
    """Presend: verify streak banner on source VOD via visual ref (OCR optional)."""
    if os.environ.get("MLBB_VOD_KILL_BANNER", "1") != "1":
        return True, "banner_check_off"
    need = min_tier if min_tier is not None else _min_tier()
    hits = scan_window(vod, banner_sec - 2.0, banner_sec + 3.0, focus_sec=banner_sec, deep=True)
    for hit in hits:
        if _hit_qualifies(hit, min_tier=need):
            return True, f"source_banner_ok:{hit.source}:{hit.label}@{hit.sec:.1f}s"
    if hits and not _banner_required():
        return True, f"source_banner_weak:{hits[0].label}"
    return False, f"source_banner_missing_min_tier={need}"


def verify_rendered_clip(
    path: Path,
    *,
    min_tier: int | None = None,
    banner_sec: float | None = None,
    clip_start: float | None = None,
) -> tuple[bool, str]:
    """Presend: streak banner must appear inside rendered mp4 (ref primary)."""
    if os.environ.get("MLBB_VOD_KILL_BANNER", "1") != "1":
        return True, "banner_check_off"
    from smart_video_editor import ffprobe_duration

    dur = ffprobe_duration(path)
    if dur < 1.0:
        return False, "clip_too_short"
    need = min_tier if min_tier is not None else _min_tier()

    if banner_sec is not None and clip_start is not None:
        offset = max(0.0, float(banner_sec) - float(clip_start))
        t0 = max(0.0, offset - 2.5)
        t1 = min(dur, offset + 3.5)
    else:
        mid = dur * 0.42
        t0 = max(0.0, mid - 4.0)
        t1 = min(dur, mid + 4.0)

    hits = scan_window(path, t0, t1, deep=True)
    for hit in hits:
        if _hit_qualifies(hit, min_tier=need):
            return True, f"banner_ok:{hit.source}:{hit.label}@{hit.sec:.1f}s"
    if _color_only_allowed():
        for hit in hits:
            if hit.tier >= need:
                return True, f"banner_ok:{hit.label}@{hit.sec:.1f}s:{hit.source}"
    if hits and not _banner_required():
        return True, f"banner_weak:{hits[0].label}"
    return False, f"banner_missing_min_tier={need}"


def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Scan MLBB VOD for kill banners.")
    parser.add_argument("video", type=Path)
    parser.add_argument("--peak", type=float, required=True)
    args = parser.parse_args()
    hit = find_banner_near_peak(args.video, args.peak)
    print(json.dumps(hit.__dict__ if hit else {}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
