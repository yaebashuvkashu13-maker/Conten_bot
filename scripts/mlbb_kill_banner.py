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
    # Strong singles only — weak "kill" alone is handled as single_weak needing color.
    (
        re.compile(
            r"has\s+slain|been\s+slain|killing\s+spree|first\s+blood|"
            r"shutdown|rampage|"
            r"убил|убийств|первая\s+кровь|серия\s+убий",
            re.I,
        ),
        1,
        "single",
    ),
    (re.compile(r"\bkill\b", re.I), 1, "single_weak"),
]

_SINGLE_STRONG_RE = re.compile(
    r"has\s+slain|been\s+slain|killing\s+spree|first\s+blood|shutdown|rampage|"
    r"убил|убийств|первая\s+кровь|серия\s+убий",
    re.I,
)


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


def _may_trust_discover_banner(row: dict) -> bool:
    """
    Blind-trust discover only for strong ref-backed multi-kills.

    Default OFF (also honors MLBB_VOD_PRESEND_TRUST_DISCOVERY). Never trust
    OCR singles — that shipped asSYCsoCSPs_959 with no real kill.
    """
    trust_raw = os.environ.get(
        "MLBB_VOD_BANNER_PRESEND_TRUST_DISCOVER",
        os.environ.get("MLBB_VOD_PRESEND_TRUST_DISCOVERY", "0"),
    )
    if str(trust_raw).strip() not in {"1", "true", "True", "yes"}:
        return False
    if not (row.get("kill_banner") or row.get("kill_banner_tier")):
        return False
    try:
        tier_i = int(row.get("kill_banner_tier") or 0)
    except (TypeError, ValueError):
        tier_i = 0
    label = str(row.get("kill_banner") or "").lower()
    src = str(
        row.get("banner_source")
        or row.get("kill_banner_source")
        or (row.get("clip") or {}).get("banner_source")
        or ""
    )
    if tier_i <= 1 or label in {"single", "single_weak", "color", "announce"}:
        return False
    if src.startswith("ocr") or src.startswith("color"):
        return False
    return True


def send_min_tier() -> int:
    """
    Minimum banner tier allowed to SEND (presend floor).

    Soften may widen OCR search but must not ship OCR 'single' FPs unless
    MLBB_ADAPTIVE_ALLOW_SINGLE=1 / MLBB_BANNER_SEND_MIN_TIER=single.
    """
    raw = (os.environ.get("MLBB_BANNER_SEND_MIN_TIER") or "").strip().lower()
    if raw:
        if raw.isdigit():
            return max(1, int(raw))
        return {"single": 1, "double": 2, "triple": 3, "maniac": 4, "savage": 5}.get(raw, 2)
    # Default floor: double, even if discover soften temporarily set min_tier=single.
    floor = 2
    if os.environ.get("MLBB_ADAPTIVE_ALLOW_SINGLE", "0") == "1":
        floor = 1
    return max(_min_tier(), floor)


def _banner_required() -> bool:
    return os.environ.get("MLBB_KILL_BANNER_REQUIRED", "1") == "1"


def _banner_hit_source_ok(source: str) -> bool:
    """OCR or screenshot-bank (ref) hits qualify for discover / presend / prefilter."""
    src = str(source or "")
    return src.startswith("ocr") or src.startswith("ref")


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
    combined = cv2.bitwise_or(gold, white)
    ratio = float(np.count_nonzero(combined)) / float(combined.size)
    return min(1.0, ratio * 11.0)


def _ocr_banner_zones(frame, *, deep: bool = False) -> str:
    import cv2

    try:
        import pytesseract
    except ImportError:
        return ""

    # Normalize to a stable canvas, then upscale OCR crops — Tesseract is often
    # blind on raw 480p banner text (gold outline / small glyphs).
    small = cv2.resize(frame, (480, 270))
    h, w = small.shape[:2]
    zones = [
        small[int(h * 0.02) : int(h * 0.28), int(w * 0.10) : int(w * 0.90)],
        small[int(h * 0.04) : int(h * 0.32), int(w * 0.18) : int(w * 0.82)],
    ]
    if deep:
        zones.append(small[int(h * 0.08) : int(h * 0.38), int(w * 0.02) : int(w * 0.38)])
    upscale = max(1.0, float(os.environ.get("MLBB_KILL_BANNER_OCR_UPSCALE", "2.0")))
    texts: list[str] = []
    psms = (7, 8, 6) if deep else (7,)
    for zone in zones:
        if zone.size == 0:
            continue
        if upscale > 1.01:
            zone = cv2.resize(
                zone,
                (max(8, int(zone.shape[1] * upscale)), max(8, int(zone.shape[0] * upscale))),
                interpolation=cv2.INTER_CUBIC,
            )
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


def _classify_frame(
    sec: float,
    frame,
    *,
    deep: bool = False,
    allow_ocr: bool = True,
) -> KillBannerHit | None:
    """
    Classify a frame as a kill-streak banner.

    Prefer the owner screenshot bank (fast) before Tesseract OCR (slow / often blind).
    """
    color = _announce_color_score(frame)
    # Ref needs a real gold announce flash — half-threshold was matching farming HUD.
    ref_color_gate = _color_min_score() * float(os.environ.get("MLBB_BANNER_REF_COLOR_MUL", "1.25"))
    if os.environ.get("MLBB_BANNER_REF_BEFORE_OCR", "1") == "1" and color >= ref_color_gate:
        try:
            from mlbb_banner_ref_match import classify_banner_reference

            ref_hit = classify_banner_reference(sec, frame)
            if ref_hit is not None:
                return ref_hit
        except Exception as exc:
            log.debug("banner ref match failed: %s", exc)

    if not allow_ocr:
        return None

    def _accept_ocr_hit(classified: KillBannerHit) -> KillBannerHit | None:
        """
        OCR alone is noisy on YT compressions. Bare 'kill' in HUD/subtitles is a
        common FP (asSYCsoCSPs_959). Require a strong single phrase, or double+.
        """
        text = str(classified.text or "")
        # Garbled OCR: too few letters relative to junk.
        letters = sum(ch.isalpha() for ch in text)
        if letters < int(os.environ.get("MLBB_BANNER_OCR_MIN_LETTERS", "8")) and classified.tier <= 1:
            return None
        if classified.tier >= 2:
            return KillBannerHit(
                sec=round(sec, 2),
                tier=classified.tier,
                label=classified.label if classified.label != "single_weak" else "single",
                text=text[:120],
                source="ocr",
            )
        # Tier-1: strong phrase only (has been slain / first blood / …).
        if classified.label == "single_weak" or not _SINGLE_STRONG_RE.search(text):
            if os.environ.get("MLBB_BANNER_OCR_WEAK_SINGLE", "0") != "1":
                return None
            need = _color_min_score() * float(
                os.environ.get("MLBB_KILL_BANNER_WEAK_COLOR_MUL", "1.15")
            )
            if color < need:
                return None
        return KillBannerHit(
            sec=round(sec, 2),
            tier=1,
            label="single",
            text=text[:120],
            source="ocr",
        )

    classified = classify_banner_text(_ocr_banner_zones(frame, deep=deep))
    if classified is not None:
        hit = _accept_ocr_hit(classified)
        if hit is not None:
            return hit
    if color >= _color_min_score():
        deep_text = _ocr_banner_zones(frame, deep=True)
        if _ENEMY_STREAK_RE.search(deep_text):
            return None
        classified = classify_banner_text(deep_text)
        if classified is not None:
            hit = _accept_ocr_hit(classified)
            if hit is not None:
                return hit
        # Color-only without readable streak text — try ref bank once more, else drop.
        try:
            from mlbb_banner_ref_match import classify_banner_reference

            ref_hit = classify_banner_reference(sec, frame)
            if ref_hit is not None:
                return ref_hit
        except Exception:
            pass
        if os.environ.get("MLBB_KILL_BANNER_COLOR_ONLY", "0") == "1":
            return KillBannerHit(
                sec=round(sec, 2),
                tier=1,
                label="color",
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
    allow_ocr: bool = True,
) -> list[KillBannerHit]:
    """Scan [t0, t1] for kill-streak banners; color prefilter then ref/OCR on candidates."""
    if quick:
        deep = False
        span = max(0.0, t1 - t0)
        sample_count = max(4, min(8, int(span / 0.4) + 1))
        frames = _ffmpeg_sample_frames(vod, t0, t1, sample_count)
        if not frames:
            frames = _sample_frames(vod, t0, t1)[:8]
        max_ocr = max(2, int(os.environ.get("MLBB_KILL_BANNER_QUICK_MAX_OCR", "3")))
    else:
        frames = _sample_frames(vod, t0, t1)
        max_ocr = 6 if deep else 4
    hits: list[KillBannerHit] = []
    frame_map = {sec: frame for sec, frame in frames}
    for sec in _candidate_secs(frames, focus_sec=focus_sec, max_ocr=max_ocr):
        frame = frame_map.get(sec)
        if frame is None:
            continue
        hit = _classify_frame(sec, frame, deep=deep, allow_ocr=allow_ocr)
        if hit is not None:
            hits.append(hit)
    if not hits and frames and not quick and allow_ocr:
        for sec, frame in frames:
            hit = _classify_frame(sec, frame, deep=True, allow_ocr=True)
            if hit is not None and _banner_hit_source_ok(hit.source):
                hits.append(hit)
                break
    hits.sort(key=lambda h: (-h.tier, 0 if _banner_hit_source_ok(h.source) else 1, h.sec))
    return hits


def find_banner_near_peak(vod: Path, peak_sec: float, *, quick: bool = False) -> KillBannerHit | None:
    """Look for streak banner around motion peak (banner at/just after peak)."""
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
    return base


def _dense_scan_enabled() -> bool:
    return os.environ.get("MLBB_VOD_BANNER_DENSE_SEC", "0") == "1"


def _discover_scan_start(vod: Path, duration: float) -> float:
    """Earliest sec to scan — title-promised savage fights often start in first 2–3 min."""
    try:
        from mlbb_vod_title import title_scan_start_sec, vod_title_blob

        blob = vod_title_blob(vod)
        title_start = title_scan_start_sec(blob, duration)
        if title_start is not None:
            return float(title_start)
    except Exception:
        pass
    return _adaptive_banner_scan_start(vod, duration)


def _title_min_tier_override() -> int:
    raw = os.environ.get("MLBB_VOD_TITLE_MIN_TIER", "").strip()
    if raw.isdigit():
        return max(0, int(raw))
    return 0


def _effective_discover_min_tier(min_tier: int | None) -> int:
    need = min_tier if min_tier is not None else _min_tier()
    title_need = _title_min_tier_override()
    return max(need, title_need) if title_need > 0 else need


def _discover_hit_target() -> int:
    """
    How many banners discover should try to collect before stopping early.

    MIN_HITS alone was too low with SEND_ALL_BANNERS — spike sweep stopped after
    2 hits and skipped the rest of the VOD.
    """
    want = max(1, int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_MIN_HITS", "2")))
    if os.environ.get("MLBB_VOD_SEND_ALL_BANNERS", "1") != "1":
        return want
    target = int(
        os.environ.get(
            "MLBB_KILL_BANNER_DISCOVER_TARGET",
            os.environ.get("MLBB_VOD_MAX_PER_VOD", "5"),
        )
    )
    return max(want, target)


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
    dense = _dense_scan_enabled()
    need = _effective_discover_min_tier(min_tier)
    # Ref-first discover is cheap — allow more probes so screenshot bank covers the VOD.
    default_probes = "28" if os.environ.get("MLBB_BANNER_REF_MATCH", "1") == "1" else "16"
    if dense:
        scan_span = max(60.0, duration - _discover_scan_start(vod, duration))
        max_probes = max(
            int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_MAX_PROBES", "96")),
            int(scan_span) + 16,
            min(1800, int(duration) + 32),
        )
        max_sec = max(120.0, float(os.environ.get("MLBB_KILL_BANNER_DISCOVER_MAX_SEC", "900")))
        dense_step = min(1.0, float(os.environ.get("MLBB_KILL_BANNER_DISCOVER_STEP", "1.0")))
    else:
        max_probes = max(4, int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_MAX_PROBES", default_probes)))
        max_sec = max(30.0, float(os.environ.get("MLBB_KILL_BANNER_DISCOVER_MAX_SEC", "120")))
        dense_step = 1.0
    deadline = time.monotonic() + max_sec
    hits: list[KillBannerHit] = []
    probes = 0
    want = _discover_hit_target()

    def _merge_hit(hit: KillBannerHit) -> None:
        if hit.tier < need or not _banner_hit_source_ok(hit.source):
            return
        if hits and hit.sec - hits[-1].sec < 6.0:
            if hit.tier > hits[-1].tier:
                hits[-1] = hit
        else:
            hits.append(hit)

    def _probe_at(t: float, *, deep: bool, allow_ocr: bool = True) -> bool:
        nonlocal probes
        if probes >= max_probes or time.monotonic() >= deadline:
            return False
        probes += 1
        # Wider probe window catches banners slightly after the motion spike.
        half = float(os.environ.get("MLBB_KILL_BANNER_DISCOVER_PROBE_AFTER", "3.0"))
        for hit in scan_window(
            vod, t - 0.75, t + max(2.5, half), focus_sec=t, deep=deep, allow_ocr=allow_ocr
        ):
            _merge_hit(hit)
            if hits:
                return True
        return probes < max_probes and time.monotonic() < deadline

    # Seed with owner-confirmed kill times when labels exist for this VOD.
    peak_hints: list[float] = list(hint_peaks or [])
    try:
        from mlbb_owner_learning import owner_kill_anchor_secs_for_path

        anchors = owner_kill_anchor_secs_for_path(vod)
        if anchors:
            peak_hints = list(dict.fromkeys([*anchors, *peak_hints]))
            log.info("banner discover %s: owner anchors=%s", vod.name, len(anchors))
    except Exception as exc:
        log.debug("owner kill anchors unavailable: %s", exc)

    # Phase 1: narrow scan around stage1 motion peaks (fast), then full retry
    # on a few misses — quick windows often stop just before the banner flash.
    peak_limit = max(4, int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_PEAK_HINTS", "8")))
    full_retry = max(0, int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_PEAK_FULL_RETRY", "3")))
    missed_peaks: list[float] = []
    for peak in list(dict.fromkeys(peak_hints))[:peak_limit]:
        if probes >= max_probes or time.monotonic() >= deadline:
            break
        probes += 1
        hit = find_banner_near_peak(vod, peak, quick=True)
        if hit:
            _merge_hit(hit)
        else:
            missed_peaks.append(peak)
    for peak in missed_peaks[:full_retry]:
        if probes >= max_probes or time.monotonic() >= deadline:
            break
        if len(hits) >= want and not dense:
            break
        probes += 1
        hit = find_banner_near_peak(vod, peak, quick=False)
        if hit:
            _merge_hit(hit)

    # Dense 1 Hz sweep for title-promised savage/maniac VODs (or explicit ops flag).
    if dense and probes < max_probes and time.monotonic() < deadline:
        t0 = _discover_scan_start(vod, duration)
        span = max(8.0, duration - t0 - 2.0)
        # Default 2s step keeps title rescans practical; set DISCOVER_STEP=1 for true 1 Hz.
        dense_step = max(
            1.0,
            float(os.environ.get("MLBB_KILL_BANNER_DISCOVER_STEP", "2.0")),
        )
        log.info(
            "banner discover %s: dense_1hz start=%.0fs span=%.0fs step=%.1fs max_probes=%s max_sec=%.0f need_tier=%s",
            vod.name,
            t0,
            span,
            dense_step,
            max_probes,
            max_sec,
            need,
        )
        from gameplay_gate import _read_frame_at
        import cv2

        color_floor = _color_min_score() * float(
            os.environ.get("MLBB_KILL_BANNER_DENSE_COLOR_MUL", "0.65")
        )
        # Title-promised maniac/savage: force periodic OCR even when gold flash is weak
        # (white/EN banners often sit under the color floor and were skipped forever).
        title_ocr_every = 0
        if need >= 4:
            title_ocr_every = max(
                0,
                int(os.environ.get("MLBB_KILL_BANNER_TITLE_OCR_EVERY", "4")),
            )
        # Reuse one OpenCV capture for H.264; fall back to per-seek ffmpeg for AV1/VP9.
        dense_cap = None
        try:
            from video_frame_io import prefer_ffmpeg_decode

            if not prefer_ffmpeg_decode(vod):
                dense_cap = cv2.VideoCapture(str(vod))
                if dense_cap is not None and not dense_cap.isOpened():
                    dense_cap.release()
                    dense_cap = None
        except Exception:
            dense_cap = None
        t = t0
        step_i = 0
        try:
            while t < duration - 2.0 and probes < max_probes and time.monotonic() < deadline:
                probes += 1
                frame = _read_frame_at(vod, t, dense_cap)
                if frame is not None:
                    color = _announce_color_score(frame)
                    force_ocr = title_ocr_every > 0 and (step_i % title_ocr_every == 0)
                    if color >= color_floor or force_ocr:
                        # Ref/color-cheap first; OCR on stronger flashes or title cadence.
                        hit = _classify_frame(t, frame, deep=False, allow_ocr=False)
                        if hit is None and (
                            force_ocr or color >= color_floor * 1.15
                        ):
                            hit = _classify_frame(t, frame, deep=False, allow_ocr=True)
                        if hit is not None:
                            _merge_hit(hit)
                if step_i % 30 == 0 or step_i < 3:
                    log.info(
                        "banner discover %s: dense t=%.0fs probes=%s/%s hits=%s",
                        vod.name,
                        t,
                        probes,
                        max_probes,
                        len(hits),
                    )
                t += dense_step
                step_i += 1
        finally:
            if dense_cap is not None:
                dense_cap.release()
        hits.sort(key=lambda h: h.sec)
        ref_n = sum(1 for h in hits if str(h.source).startswith("ref"))
        ocr_n = sum(1 for h in hits if str(h.source).startswith("ocr"))
        log.info(
            "banner discover %s: dense=%s probes=%s hits=%s/%s need_tier=%s (ref=%s ocr=%s)",
            vod.name,
            dense,
            probes,
            len(hits),
            want,
            need,
            ref_n,
            ocr_n,
        )
        return hits

    force_full = os.environ.get("MLBB_VOD_BANNER_DISCOVER_FULL", "0") == "1"
    # Bounded spike sweep when peaks-only is thin — finds banners motion peaks miss
    # without the hours-long full-VOD OCR path. With SEND_ALL, `want` is the
    # per-VOD target (not just MIN_HITS=2), so we keep sweeping until budget.
    need_spike = force_full or (
        os.environ.get("MLBB_VOD_BANNER_DISCOVER_SPIKE", "1") == "1" and len(hits) < want
    )
    if not need_spike:
        hits.sort(key=lambda h: h.sec)
        log.info(
            "banner discover %s: peaks-only probes=%s hits=%s target=%s need_tier=%s",
            vod.name,
            probes,
            len(hits),
            want,
            need,
        )
        return hits

    # Phase 2: sparse motion-gated sweep (capped by remaining probes/deadline).
    # First pass is screenshot-bank only (fast); OCR only if still thin.
    if probes < max_probes and time.monotonic() < deadline:
        win = float(analysis.get("window_seconds", 2.0))
        motion = np.asarray(_analysis_series(analysis, "center_motion"), dtype=np.float32)
        audio = np.asarray(_analysis_series(analysis, "audio"), dtype=np.float32)
        combined = motion if audio.size != motion.size else motion * 0.55 + audio * 0.45
        # Ref-only can afford more spikes than OCR (~15s/probe).
        spike_cap = max(
            4,
            int(
                os.environ.get(
                    "MLBB_KILL_BANNER_DISCOVER_SPIKE_CAP",
                    "24" if os.environ.get("MLBB_BANNER_REF_MATCH", "1") == "1" else "10",
                )
            ),
        )
        if combined.size > 8:
            spike_pct = float(os.environ.get("MLBB_KILL_BANNER_DISCOVER_SPIKE_PCT", "65"))
            spike_pct = max(50.0, min(95.0, spike_pct))
            thr = float(np.percentile(combined, spike_pct))
            idxs = [i for i, v in enumerate(combined) if float(v) >= thr]
            if len(idxs) > spike_cap:
                step_i = max(1, len(idxs) // spike_cap)
                idxs = idxs[::step_i][:spike_cap]
        else:
            idxs = list(range(combined.size))
        t0 = _discover_scan_start(vod, duration)
        known = {round(h.sec / 4.0) for h in hits}

        def _run_spike_pass(*, allow_ocr: bool, label: str, stop_at: int) -> None:
            nonlocal probes
            for bi in idxs:
                if probes >= max_probes or time.monotonic() >= deadline:
                    break
                if len(hits) >= stop_at and not force_full:
                    break
                t = bi * max(win, 0.5)
                if t < t0 or t > duration - 4.0:
                    continue
                if round(t / 4.0) in known:
                    continue
                if probes % 8 == 0:
                    log.info(
                        "banner discover %s: %s probe=%s/%s t=%.0fs hits=%s/%s",
                        vod.name,
                        label,
                        probes,
                        max_probes,
                        t,
                        len(hits),
                        want,
                    )
                if not _probe_at(t, deep=False, allow_ocr=allow_ocr):
                    break
                if hits:
                    known.add(round(hits[-1].sec / 4.0))

        _run_spike_pass(allow_ocr=False, label="ref", stop_at=want)
        if len(hits) < want and probes < max_probes and time.monotonic() < deadline:
            # OCR fallback on remaining budget — more spikes when target > min hits.
            ocr_spikes = max(
                4,
                int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_OCR_SPIKES", "12")),
            )
            idxs = idxs[:ocr_spikes]
            _run_spike_pass(allow_ocr=True, label="ocr", stop_at=want)

    hits.sort(key=lambda h: h.sec)
    ref_n = sum(1 for h in hits if str(h.source).startswith("ref"))
    ocr_n = sum(1 for h in hits if str(h.source).startswith("ocr"))
    log.info(
        "banner discover %s: done probes=%s hits=%s/%s (ref=%s ocr=%s) elapsed=%.0fs need_tier=%s",
        vod.name,
        probes,
        len(hits),
        want,
        ref_n,
        ocr_n,
        max_sec - max(0.0, deadline - time.monotonic()),
        need,
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
    banner_tier: int | None = None,
) -> tuple[float, float, float]:
    """
    Clip bounds anchored on kill banner.

    End is hard-capped at last_kill_banner + MLBB_BANNER_POST_SEC (default 3s)
    so post-fight lane jogging is not kept. Extra length comes from pre-roll only.
    """
    from mlbb_fight_segment import (
        _fight_min_sec,
        _fight_max_sec,
        _fight_hard_max_sec,
        banner_lead_sec,
        banner_post_sec,
        ideal_clip_min_sec,
    )

    min_d = _fight_min_sec()
    max_d = _fight_max_sec()
    hard_max = _fight_hard_max_sec()
    lead = banner_lead_sec(banner_tier)
    post = banner_post_sec()
    banner = float(banner_sec)
    file_dur = float(file_dur)

    # Hard rule: stop shortly after the kill banner (last kill of this moment).
    end = min(file_dur, banner + post)
    if fight_start is not None and fight_end is not None and float(fight_end) > float(fight_start):
        # Keep pre-fight setup from sustain, but never follow fight_end into a run.
        start = max(0.0, min(float(fight_start), banner - lead))
    else:
        start = max(0.0, banner - lead)

    if banner < start:
        start = max(0.0, banner - lead)
    if banner > end:
        end = min(file_dur, banner + max(post, 2.0))

    dur = end - start
    need = max(min_d, ideal_clip_min_sec() if os.environ.get("MLBB_BANNER_IDEAL_MIN", "1") == "1" else min_d)

    # Prefer longer pre-roll over longer post — never grow past banner+post
    # unless the file literally starts at the banner (can't pull start left).
    if dur < need:
        start = max(0.0, end - need)
        if banner < start:
            start = max(0.0, banner - lead)
        dur = end - start
    if dur < min_d:
        # Only stretch end when we cannot get min_d from pre-roll (early-file banner).
        deficit = min_d - dur
        if start <= 0.05 and deficit > 0.05:
            end = min(file_dur, end + deficit)
            dur = end - start
        else:
            start = max(0.0, end - min_d)
            if banner < start:
                start = max(0.0, banner - lead)
                # Keep hard post cap when pre-roll is available.
                end = min(file_dur, max(end, banner + post))
            dur = end - start

    if dur > hard_max:
        start = max(0.0, end - hard_max)
        if banner < start:
            start = max(0.0, banner - lead)
            end = min(file_dur, start + hard_max)
        dur = end - start
    elif dur > max_d:
        start = max(0.0, end - max_d)
        if banner < start:
            start = max(0.0, banner - lead)
            end = min(file_dur, start + max_d)
        dur = end - start

    # Banner must not sit in the last ~40% — pull start earlier, keep post short.
    banner_rel_max = float(os.environ.get("MLBB_BANNER_MAX_REL_POS", "0.58"))
    banner_rel = (banner - start) / max(dur, 1e-6)
    if dur >= 10.0 and banner_rel > banner_rel_max:
        pre = max(lead, min_d * 0.55)
        start = max(0.0, banner - pre)
        end = min(file_dur, banner + post)
        dur = end - start
        if dur < min_d and start <= 0.05:
            end = min(file_dur, start + min_d)
            dur = end - start

    # Final guarantee: unless early-file forced a min stretch, end ≤ banner+post.
    hard_end = min(file_dur, banner + post)
    if end > hard_end + 0.35 and start > 0.05:
        end = hard_end
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
            start, end, dur = bounds_from_banner(
                hit.sec,
                file_dur,
                fight_start=fight_start,
                fight_end=fight_end,
                banner_tier=hit.tier,
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

    start, end, dur = bounds_from_banner(
        hit.sec,
        file_dur,
        fight_start=fight_start,
        fight_end=fight_end,
        banner_tier=hit.tier,
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
            return True, f"source_banner_ok:{hit.label}@{hit.sec:.1f}s:{hit.source}"
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
            return True, f"banner_ok:{hit.label}@{hit.sec:.1f}s:{hit.source}"
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
