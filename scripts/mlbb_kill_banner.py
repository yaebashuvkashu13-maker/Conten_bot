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
    (re.compile(r"savage|саваж|saa?x?e|sav.?g", re.I), 5, "savage"),
    (re.compile(r"legendary|легендар|legenda", re.I), 5, "legendary"),
    (re.compile(r"maniac|маньяк|man1ac|mani.?ac", re.I), 4, "maniac"),
    (re.compile(r"ruthless|беспощад|безжалост", re.I), 4, "ruthless"),
    (re.compile(r"triple\s*kill|тройн.{0,12}убий|tripl|tr1ple", re.I), 3, "triple"),
    (re.compile(r"ultra\s*kill", re.I), 3, "triple"),
    (
        re.compile(
            r"double\s*kill|двойн.{0,12}убий|ou?ble\s*kill|d0uble|2\s*x\s*kill|doub.?e|doubl",
            re.I,
        ),
        2,
        "double",
    ),
    (re.compile(r"\bkill\b|убийств|ki11|k1ll", re.I), 1, "single"),
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
    return os.environ.get("MLBB_KILL_BANNER_REQUIRED", "1") == "1"


def _motion_anchor_ok() -> bool:
    """Motion fight bounds are acceptable without a verified kill-banner anchor."""
    if os.environ.get("MLBB_VOD_MOTION_ANCHOR_OK", "0") == "1":
        return True
    if not _banner_required():
        return True
    if os.environ.get("MLBB_VOD_BANNER_PRESEND", "1") != "1":
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


def _banner_hit_source_ok(source: str) -> bool:
    return source in ("ocr", "ref")


def _announce_color_score(frame) -> float:
    import cv2
    import numpy as np

    small = cv2.resize(frame, (320, 180))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    h, w = small.shape[:2]
    zone = hsv[int(h * 0.02) : int(h * 0.30), int(w * 0.15) : int(w * 0.85)]
    if zone.size == 0:
        return 0.0
    gold = cv2.inRange(zone, np.array([8, 100, 140]), np.array([40, 255, 255]))
    white = cv2.inRange(zone, np.array([0, 0, 210]), np.array([180, 50, 255]))
    cyan = cv2.inRange(zone, np.array([85, 70, 120]), np.array([115, 255, 255]))
    combined = cv2.bitwise_or(cv2.bitwise_or(gold, white), cyan)
    ratio = float(np.count_nonzero(combined)) / float(combined.size)
    return min(1.0, ratio * 11.0)


def _ocr_banner_zones(frame, *, deep: bool = False) -> str:
    import cv2

    try:
        import pytesseract
    except ImportError:
        return ""

    target = (960, 540) if deep else (640, 360)
    small = cv2.resize(frame, target)
    h, w = small.shape[:2]
    zones = [
        small[int(h * 0.02) : int(h * 0.28), int(w * 0.10) : int(w * 0.90)],
        small[int(h * 0.04) : int(h * 0.32), int(w * 0.18) : int(w * 0.82)],
    ]
    if deep:
        zones.append(small[int(h * 0.08) : int(h * 0.38), int(w * 0.02) : int(w * 0.38)])
    texts: list[str] = []
    psms = (7, 8, 6, 13) if deep else (7, 8)
    for zone in zones:
        if zone.size == 0:
            continue
        gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
        variants = [cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]]
        if deep:
            variants.append(cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1])
            variants.append(cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5))
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
                    if classify_banner_text(text) is not None:
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


def _classify_frame(sec: float, frame, *, deep: bool = False) -> KillBannerHit | None:
    classified = classify_banner_text(_ocr_banner_zones(frame, deep=deep))
    if classified is not None:
        return KillBannerHit(
            sec=round(sec, 2),
            tier=classified.tier,
            label=classified.label,
            text=classified.text,
            source="ocr",
        )
    color = _announce_color_score(frame)
    if color >= _color_min_score():
        deep_text = _ocr_banner_zones(frame, deep=True)
        if _ENEMY_STREAK_RE.search(deep_text):
            return None
        if classify_banner_text(deep_text) is not None:
            classified = classify_banner_text(deep_text)
            assert classified is not None
            return KillBannerHit(
                sec=round(sec, 2),
                tier=classified.tier,
                label=classified.label,
                text=classified.text,
                source="ocr",
            )
        if not _color_only_allowed():
            if color >= _color_min_score() * 0.55:
                try:
                    from mlbb_banner_ref_match import classify_banner_reference

                    ref_hit = classify_banner_reference(sec, frame)
                    if ref_hit is not None:
                        return ref_hit
                except Exception:
                    pass
            deep_text = _ocr_banner_zones(frame, deep=True)
            if classify_banner_text(deep_text) is not None:
                classified = classify_banner_text(deep_text)
                assert classified is not None
                return KillBannerHit(
                    sec=round(sec, 2),
                    tier=classified.tier,
                    label=classified.label,
                    text=classified.text,
                    source="ocr",
                )
            return None
        return KillBannerHit(
            sec=round(sec, 2),
            tier=3,
            label="announce",
            text=f"color={color:.3f}",
            source="color",
        )
    return None


def _color_only_allowed() -> bool:
    return os.environ.get("MLBB_KILL_BANNER_COLOR_ONLY", "0") == "1"


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
        ordered = sorted(frames, key=lambda row: abs(row[0] - focus_sec))
        nearest = ordered[0][0]
        second = ordered[1][0] if len(ordered) > 1 else None
        # Always include focus-adjacent frames even when color prefilter is weak.
        if nearest not in picks:
            picks.insert(0, nearest)
        if second is not None and second not in picks and len(picks) < max_ocr + 1:
            picks.insert(1, second)
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
    """Scan [t0, t1] for kill-streak banners; color prefilter then OCR on candidates."""
    if quick:
        deep = False
        span = max(0.0, t1 - t0)
        sample_count = max(3, min(6, int(span / 0.5) + 1))
        frames = _ffmpeg_sample_frames(vod, t0, t1, sample_count)
        if not frames:
            frames = _sample_frames(vod, t0, t1)[:6]
        # quick mode: allow one extra OCR attempt to avoid missing short banners.
        max_ocr = 3
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
            if hit is not None and _banner_hit_source_ok(hit.source):
                hits.append(hit)
                break
    hits.sort(key=lambda h: (-h.tier, 0 if _banner_hit_source_ok(h.source) else 1, h.sec))
    return hits


def find_banner_near_peak(vod: Path, peak_sec: float, *, quick: bool = False) -> KillBannerHit | None:
    """Look for streak banner around motion peak (banner at/just after peak)."""
    frame = _read_frame(vod, peak_sec)
    if frame is not None:
        hit = _classify_frame(peak_sec, frame, deep=True)
        if hit and hit.tier >= _min_tier() and _banner_hit_source_ok(hit.source):
            return hit
    if quick:
        before = float(os.environ.get("MLBB_KILL_BANNER_QUICK_BEFORE", "10"))
        after = float(os.environ.get("MLBB_KILL_BANNER_QUICK_AFTER", "6"))
        hits = scan_window(vod, peak_sec - before, peak_sec + after, focus_sec=peak_sec, quick=True)
    else:
        before = float(os.environ.get("MLBB_KILL_BANNER_SCAN_BEFORE", "20"))
        after = float(os.environ.get("MLBB_KILL_BANNER_SCAN_AFTER", "10"))
        hits = scan_window(vod, peak_sec - before, peak_sec + after, focus_sec=peak_sec)
    if not hits:
        return None
    min_tier = _min_tier()
    for hit in hits:
        if hit.tier >= min_tier and _banner_hit_source_ok(hit.source):
            return hit
    if not _banner_required():
        for hit in hits:
            if hit.tier >= min_tier:
                return hit
    return None


def _adaptive_banner_scan_start(vod: Path, duration: float) -> float:
    """Earliest sec to scan for banners — short VODs have fights before 5 min."""
    base = float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "300"))
    if duration <= 240:
        return 15.0
    if duration <= 480:
        return min(base, 90.0)
    if duration <= 900:
        return min(base, 120.0)
    return base


def _stratified_peak_hints(peaks: list[float], limit: int) -> list[float]:
    """Sample motion peaks across the full timeline — not only early laning."""
    ordered = sorted(set(float(p) for p in peaks))
    if len(ordered) <= limit:
        return ordered
    if limit <= 1:
        return [ordered[-1]]
    slots = [int(round(i * (len(ordered) - 1) / (limit - 1))) for i in range(limit)]
    return [ordered[i] for i in sorted(set(slots))]


def discover_vod_kill_banners(
    vod: Path,
    *,
    min_tier: int | None = None,
    hint_peaks: list[float] | None = None,
) -> list[KillBannerHit]:
    """
    Motion-gated sparse OCR scan for kill banners independent of motion peaks.
    Capped by probe count and wall time — full-VOD deep OCR can stall for hours.
    """
    if os.environ.get("MLBB_VOD_KILL_BANNER", "1") != "1":
        return []
    if os.environ.get("MLBB_VOD_BANNER_DISCOVER", "1") != "1":
        return []
    import numpy as np

    from mlbb_fight_segment import _analysis_for

    analysis = _analysis_for(vod)
    duration = float(analysis.get("duration") or 0.0)
    if duration < 20.0:
        return []
    need = min_tier if min_tier is not None else _min_tier()
    step = float(os.environ.get("MLBB_KILL_BANNER_DISCOVER_STEP", "3.0"))
    t0 = _adaptive_banner_scan_start(vod, duration)
    scan_span = max(60.0, duration - t0)
    max_probes = max(
        4,
        int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_MAX_PROBES", "16")),
        min(200, int(scan_span / max(step, 1.0)) + 8),
    )
    max_sec = max(30.0, float(os.environ.get("MLBB_KILL_BANNER_DISCOVER_MAX_SEC", "120")))
    tail_reserve = min(4, max(2, max_probes // 6))
    core_probe_cap = max(4, max_probes - tail_reserve)
    deadline = time.monotonic() + max_sec
    hits: list[KillBannerHit] = []
    probes = 0

    def _merge_hit(hit: KillBannerHit) -> None:
        if hit.tier < need or not _banner_hit_source_ok(hit.source):
            return
        from mlbb_fight_segment import banner_in_vod_tail

        if banner_in_vod_tail(vod, hit.sec):
            return
        # Do NOT gate by POV match here: some POV layouts shift the portrait,
        # and we'd rather keep banner candidates and let presend validate POV.
        if hits and hit.sec - hits[-1].sec < 6.0:
            if hit.tier > hits[-1].tier:
                hits[-1] = hit
        else:
            hits.append(hit)

    def _probe_at(t: float, *, deep: bool, quick: bool = False) -> bool:
        nonlocal probes
        if probes >= max_probes or time.monotonic() >= deadline:
            return False
        probes += 1
        if quick:
            before = float(os.environ.get("MLBB_KILL_BANNER_QUICK_BEFORE", "10"))
            after = float(os.environ.get("MLBB_KILL_BANNER_QUICK_AFTER", "6"))
        else:
            before = float(os.environ.get("MLBB_KILL_BANNER_SCAN_BEFORE", "20"))
            after = float(os.environ.get("MLBB_KILL_BANNER_SCAN_AFTER", "10"))
        for hit in scan_window(
            vod,
            t - before,
            t + after,
            focus_sec=t,
            deep=deep,
            quick=quick,
        ):
            _merge_hit(hit)
        return probes < max_probes and time.monotonic() < deadline

    def _timestep_color_probe(t: float, cap=None) -> None:
        """
        Cheap timestep scan: read ONE frame at t, use color prefilter,
        then run OCR near t only when color is promising.
        """
        nonlocal probes
        if probes >= max_probes or time.monotonic() >= deadline:
            return
        if cap is not None:
            from gameplay_gate import _read_frame_at

            frame = _read_frame_at(vod, float(t), cap)
        else:
            frame = _read_frame(vod, t)
        if frame is None:
            return
        # Same heuristic as scan_window() candidate picking.
        color = _announce_color_score(frame)
        if color < _color_min_score() * 0.75:
            return
        # Single-frame OCR only — never scan_window() here; dense 2s timestep must stay cheap.
        from gameplay_gate import _read_frame_at

        win_thr = float(os.environ.get("MLBB_KILL_BANNER_COLOR_OCR_WINDOW_MIN", "0.12"))
        offsets = (-0.35, 0.0, 0.35, 0.7) if color >= win_thr else (0.0, 0.6)

        for off in offsets:
            if probes >= max_probes or time.monotonic() >= deadline:
                return
            probes += 1
            fr = frame if off == 0.0 else (
                _read_frame_at(vod, float(t) + off, cap) if cap is not None else _read_frame(vod, float(t) + off)
            )
            if fr is None:
                continue
            hit = _classify_frame(float(t) + off, fr, deep=False)
            if hit is not None:
                _merge_hit(hit)
                return

    # Phase 1: quick OCR around motion peaks spread across the whole VOD.
    peak_limit = max(4, int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_PEAK_HINTS", "6")))
    peak_probe_cap = max(4, min(peak_limit, int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_PEAK_MAX_PROBES", "8"))))
    for peak in _stratified_peak_hints(hint_peaks or [], peak_limit):
        if probes >= peak_probe_cap or probes >= core_probe_cap or time.monotonic() >= deadline:
            break
        probes += 1
        hit = find_banner_near_peak(vod, peak, quick=True)
        if hit:
            _merge_hit(hit)

    # Phase 1b: optional tail pass (usually disabled — VOD tails are often rank/menu).
    if os.environ.get("MLBB_KILL_BANNER_TAIL_PASS", "0") == "1":
        for tail_off in (8.0, 14.0, 22.0):
            if probes >= max_probes or time.monotonic() >= deadline:
                break
            t = max(t0 + 8.0, duration - tail_off)
            frame = _read_frame(vod, t)
            if frame is None:
                continue
            color = _announce_color_score(frame)
            if color < _color_min_score() * 0.75:
                continue
            win_thr = float(os.environ.get("MLBB_KILL_BANNER_COLOR_OCR_WINDOW_MIN", "0.12"))
            offsets = (-0.35, 0.0, 0.35, 0.7) if color >= win_thr else (0.0, 0.6)
            for off in offsets:
                if probes >= max_probes or time.monotonic() >= deadline:
                    break
                probes += 1
                fr = frame if off == 0.0 else _read_frame(vod, float(t) + off)
                if fr is None:
                    continue
                hit = _classify_frame(float(t) + off, fr, deep=False)
                if hit is not None:
                    _merge_hit(hit)
                    break

    # Phase 2: evenly spaced probes across entire VOD (late savages at 10+ min).
    timestep = os.environ.get("MLBB_VOD_BANNER_TIMESTEP_SCAN", "1") == "1"
    full_sweep = os.environ.get("MLBB_VOD_BANNER_DISCOVER_FULL", "0") == "1"
    if timestep or full_sweep:
        # Instead of expensive scan_window() at every timestep, do a cheap per-step
        # color probe and OCR only on promising frames. This greatly increases recall.
        if timestep:
            # Spread sampling across the whole VOD under tight time budget.
            span = max(8.0, duration - t0 - 2.0)
            sample_cap = int(os.environ.get("MLBB_KILL_BANNER_TIMESTEP_SAMPLES", "160"))
            k = max(12, min(sample_cap, max_probes - probes, int(span / max(step, 1.0)) + 1))
            try:
                import cv2

                cap = cv2.VideoCapture(str(vod))
            except Exception:
                cap = None
            for i in range(k):
                if probes >= max_probes or time.monotonic() >= deadline:
                    break
                t = t0 + (i * span / max(k - 1, 1))
                _timestep_color_probe(t, cap=cap)
                if i in (0, k // 2, k - 1):
                    log.info(
                        "banner discover %s: timestep_sample i=%s/%s t=%.0fs probes=%s/%s hits=%s",
                        vod.name,
                        i + 1,
                        k,
                        t,
                        probes,
                        max_probes,
                        len(hits),
                    )
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
        else:
            # Fallback: keep previous strided window probes for full_sweep-only mode.
            span = max(8.0, duration - t0 - 4.0)
            remaining = max(0, core_probe_cap - probes)
            stride_count = max(remaining, int(span / max(step, 1.0)) + 1)
            stride_count = min(stride_count, remaining) if remaining else 0
            for i in range(stride_count):
                if probes >= max_probes or time.monotonic() >= deadline:
                    break
                if stride_count <= 1:
                    t = t0 + span * 0.5
                else:
                    t = t0 + i * span / (stride_count - 1)
                deep = (probes % 5) == 4
                quick = not deep
                if not _probe_at(t, deep=deep, quick=quick):
                    break
                if int(t) % 90 == 0 and int(t) > int(t0):
                    log.info(
                        "banner discover %s: strided t=%.0fs probes=%s/%s hits=%s",
                        vod.name,
                        t,
                        probes,
                        max_probes,
                        len(hits),
                    )
        if full_sweep and not timestep and probes < max_probes and time.monotonic() < deadline:
            win = float(analysis.get("window_seconds", 2.0))
            motion = np.asarray(_analysis_series(analysis, "center_motion"), dtype=np.float32)
            audio = np.asarray(_analysis_series(analysis, "audio"), dtype=np.float32)
            combined = motion if audio.size != motion.size else motion * 0.55 + audio * 0.45
            motion_thr = float(np.percentile(combined, 35)) if combined.size > 4 else 0.0
            t = t0
            while t < duration - 4.0 and probes < max_probes and time.monotonic() < deadline:
                bi = min(int(t / max(win, 0.5)), max(0, combined.size - 1))
                if combined.size > bi and float(combined[bi]) < motion_thr:
                    t += step
                    continue
                if probes % 5 == 0:
                    log.info(
                        "banner discover %s: motion_probe=%s/%s t=%.0fs hits=%s",
                        vod.name,
                        probes,
                        max_probes,
                        t,
                        len(hits),
                    )
                if not _probe_at(t, deep=False, quick=True):
                    break
                t += step
        hits.sort(key=lambda h: h.sec)
        log.info(
            "banner discover %s: timestep=%s full=%s probes=%s hits=%s",
            vod.name,
            timestep,
            full_sweep,
            probes,
            len(hits),
        )
        return hits

    hits.sort(key=lambda h: h.sec)
    log.info(
        "banner discover %s: peaks-only probes=%s hits=%s",
        vod.name,
        probes,
        len(hits),
    )
    return hits


def filter_peaks_with_ocr_banner(
    vod: Path,
    peaks: list[float],
    *,
    max_probe: int | None = None,
    known_banners: list[KillBannerHit] | None = None,
) -> list[float]:
    """Keep motion peaks that have an OCR-qualified kill banner nearby."""
    if os.environ.get("MLBB_VOD_BANNER_PREFILTER", "1") != "1":
        return peaks
    limit = max_probe or int(os.environ.get("MLBB_VOD_BANNER_PREFILTER_PEAKS", "16"))
    need = _min_tier()
    before = float(os.environ.get("MLBB_KILL_BANNER_SCAN_BEFORE", "20"))
    after = float(os.environ.get("MLBB_KILL_BANNER_SCAN_AFTER", "10"))
    qualified = [
        h
        for h in (known_banners or [])
        if h.tier >= need and _banner_hit_source_ok(h.source)
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
    ocr_cap = min(limit, int(os.environ.get("MLBB_VOD_BANNER_PREFILTER_OCR_PEAKS", "8")))
    for peak in peaks[: max(1, ocr_cap)]:
        hit = find_banner_near_peak(vod, peak, quick=True)
        if hit and _banner_hit_source_ok(hit.source) and hit.tier >= need:
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

    post = float(os.environ.get("MLBB_FIGHT_POST_SEC", os.environ.get("MLBB_BANNER_POST_SEC", "4")))
    if fight_start is not None and fight_end is not None and fight_end > fight_start:
        start = max(0.0, float(fight_start) - lead)
        end = min(float(file_dur), float(fight_end) + post)
    else:
        start = max(0.0, float(banner_sec) - lead)
        post_cap = float(os.environ.get("MLBB_BANNER_POST_SEC", str(post)))
        tail = min(post_cap, max(post, min_d * 0.45, (max_d - lead) * 0.32))
        end = min(float(file_dur), float(banner_sec) + tail)

    if float(banner_sec) < start:
        start = max(0.0, float(banner_sec) - lead)
    if float(banner_sec) > end:
        end = min(float(file_dur), float(banner_sec) + max(2.0, min_d * 0.4))

    from mlbb_fight_segment import ideal_clip_min_sec

    dur = end - start
    need = ideal_clip_min_sec()
    if dur < need:
        end = min(file_dur, start + need)
        dur = end - start
    if dur > hard_max:
        end = start + hard_max
        dur = hard_max
    elif dur > max_d:
        end = start + max_d
        dur = max_d

    # Montage: banner should not sit in the last ~40% (post-fight death / idle tail).
    banner_rel_max = float(os.environ.get("MLBB_BANNER_MAX_REL_POS", "0.58"))
    banner_rel = (float(banner_sec) - start) / max(dur, 1e-6)
    if dur >= 10.0 and banner_rel > banner_rel_max:
        post = min(float(os.environ.get("MLBB_BANNER_POST_SEC", "5")), lead * 0.85)
        pre = max(min_d - post, min_d * 0.55)
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
    Prefer kill-streak banner anchor inside motion sustain window.
    Returns None only when banner is mandatory and no qualifying streak is found.
    """
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

    if _motion_anchor_ok():
        if hit is not None and hit.tier >= min_tier:
            from mlbb_fight_segment import banner_in_vod_tail

            if banner_in_vod_tail(vod, hit.sec):
                return None
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
        return fight_start, fight_end, fight_dur, motion_meta

    if hit is None or hit.tier < min_tier:
        return None

    from mlbb_fight_segment import banner_in_vod_tail

    if banner_in_vod_tail(vod, hit.sec):
        return None

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


def verify_banner_on_source(
    vod: Path,
    banner_sec: float,
    *,
    min_tier: int | None = None,
) -> tuple[bool, str]:
    """Presend: verify streak banner on source VOD (rendered mp4 OCR is unreliable)."""
    if os.environ.get("MLBB_VOD_KILL_BANNER", "1") != "1":
        return True, "banner_check_off"
    need = min_tier if min_tier is not None else _min_tier()
    hits = scan_window(vod, banner_sec - 2.0, banner_sec + 3.0, focus_sec=banner_sec, deep=True)
    for hit in hits:
        if hit.tier >= need and _banner_hit_source_ok(hit.source):
            if os.environ.get("MLBB_BANNER_POV_MATCH", "1") == "1":
                from mlbb_banner_pov_match import banner_pov_hero_match

                pov_ok, pov_reason, _sim = banner_pov_hero_match(vod, hit.sec)
                if not pov_ok:
                    continue
            return True, f"source_banner_ok:{hit.label}@{hit.sec:.1f}s"
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
    """Presend: streak banner must appear inside rendered mp4."""
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
        if hit.tier >= need and _banner_hit_source_ok(hit.source):
            return True, f"banner_ok:{hit.label}@{hit.sec:.1f}s"
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
