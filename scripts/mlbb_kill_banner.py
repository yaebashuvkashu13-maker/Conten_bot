#!/usr/bin/env python3
"""MLBB in-game kill-streak banner detection (Triple Kill, Maniac, Savage, …)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# tier: 1=weak … 5=best. User wants triple (3) and above.
TIER_LABELS = {
    5: "savage",
    4: "maniac",
    3: "triple",
    2: "double",
    1: "single",
}

_STREAK_PATTERNS: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"savage|саваж", re.I), 5, "savage"),
    (re.compile(r"legendary|легендар", re.I), 5, "legendary"),
    (re.compile(r"maniac|маньяк", re.I), 4, "maniac"),
    (re.compile(r"ruthless|беспощад|безжалост", re.I), 4, "ruthless"),
    (re.compile(r"triple\s*kill|тройн.{0,12}убий", re.I), 3, "triple"),
    (re.compile(r"ultra\s*kill", re.I), 3, "triple"),
    (re.compile(r"double\s*kill|двойн.{0,12}убий", re.I), 2, "double"),
    (re.compile(r"\bkill\b|убийств", re.I), 1, "single"),
]


@dataclass(frozen=True)
class KillBannerHit:
    sec: float
    tier: int
    label: str
    text: str


def _min_tier() -> int:
    raw = (os.environ.get("MLBB_KILL_BANNER_MIN_TIER") or "triple").strip().lower()
    if raw.isdigit():
        return max(1, int(raw))
    return {"single": 1, "double": 2, "triple": 3, "maniac": 4, "savage": 5}.get(raw, 3)


def _banner_required() -> bool:
    return os.environ.get("MLBB_KILL_BANNER_REQUIRED", "1") == "1"


def _banner_lead_sec() -> float:
    return float(os.environ.get("MLBB_KILL_BANNER_LEAD_SEC", "10"))


def _banner_post_sec() -> float:
    return float(os.environ.get("MLBB_KILL_BANNER_POST_SEC", "14"))


def _scan_step() -> float:
    return float(os.environ.get("MLBB_KILL_BANNER_SCAN_STEP", "0.45"))


def classify_banner_text(text: str) -> KillBannerHit | None:
    blob = " ".join(str(text or "").split())
    if not blob:
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


def _ocr_center_banner(frame) -> str:
    import cv2

    try:
        import pytesseract
    except ImportError:
        return ""
    small = cv2.resize(frame, (480, 270))
    h, w = small.shape[:2]
    # MLBB streak banner: upper-center overlay.
    zone = small[int(h * 0.28) : int(h * 0.58), int(w * 0.12) : int(w * 0.88)]
    gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
    gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    text = pytesseract.image_to_string(
        gray,
        config="--psm 7 -l eng+rus",
    )
    return " ".join(text.split())


def _read_frame(vod: Path, sec: float):
    from gameplay_gate import _read_frame_at

    return _read_frame_at(vod, sec)


def scan_window(vod: Path, t0: float, t1: float) -> list[KillBannerHit]:
    """OCR-scan [t0, t1] for kill-streak banners; return hits tier-sorted best first."""
    step = max(0.25, _scan_step())
    hits: list[KillBannerHit] = []
    t = max(0.0, t0)
    end = max(t, t1)
    while t <= end:
        frame = _read_frame(vod, t)
        if frame is not None:
            classified = classify_banner_text(_ocr_center_banner(frame))
            if classified is not None:
                hits.append(
                    KillBannerHit(
                        sec=round(t, 2),
                        tier=classified.tier,
                        label=classified.label,
                        text=classified.text,
                    )
                )
        t += step
    hits.sort(key=lambda h: (-h.tier, h.sec))
    return hits


def find_banner_near_peak(vod: Path, peak_sec: float) -> KillBannerHit | None:
    """Look for streak banner around motion peak (banner usually at/just after peak)."""
    before = float(os.environ.get("MLBB_KILL_BANNER_SCAN_BEFORE", "12"))
    after = float(os.environ.get("MLBB_KILL_BANNER_SCAN_AFTER", "4"))
    hits = scan_window(vod, peak_sec - before, peak_sec + after)
    if not hits:
        return None
    min_tier = _min_tier()
    for hit in hits:
        if hit.tier >= min_tier:
            return hit
    return hits[0] if not _banner_required() else None


def bounds_from_banner(
    banner_sec: float,
    file_dur: float,
    *,
    lead_sec: float | None = None,
    post_sec: float | None = None,
) -> tuple[float, float, float]:
    lead = _banner_lead_sec() if lead_sec is None else lead_sec
    post = _banner_post_sec() if post_sec is None else post_sec
    from mlbb_fight_segment import _fight_min_sec, _fight_max_sec, _fight_hard_max_sec

    min_d = _fight_min_sec()
    max_d = min(_fight_max_sec(), lead + post)
    hard_max = _fight_hard_max_sec()

    start = max(0.0, float(banner_sec) - lead)
    end = min(float(file_dur), float(banner_sec) + post)
    dur = end - start
    if dur < min_d:
        end = min(file_dur, start + min_d)
        dur = end - start
    cap = min(max_d, hard_max)
    if dur > cap:
        end = start + cap
        dur = cap
    return round(start, 2), round(end, 2), round(dur, 2)


def resolve_fight_bounds(
    vod: Path,
    peak_sec: float,
    file_dur: float,
) -> tuple[float, float, float, dict] | None:
    """
    Prefer kill-streak banner anchor: start = banner - 10s, short post-banner fight.
    Returns None when banner required but only single/double/none found.
    """
    if os.environ.get("MLBB_VOD_KILL_BANNER", "1") != "1":
        from mlbb_fight_segment import detect_fight_bounds

        start, end, dur = detect_fight_bounds(vod, peak_sec)
        return start, end, dur, {"anchor": "motion", "banner_sec": peak_sec}

    hit = find_banner_near_peak(vod, peak_sec)
    min_tier = _min_tier()
    if hit is None:
        if _banner_required():
            return None
        from mlbb_fight_segment import detect_fight_bounds

        start, end, dur = detect_fight_bounds(vod, peak_sec)
        return start, end, dur, {"anchor": "motion", "banner_sec": peak_sec}

    if hit.tier < min_tier:
        if _banner_required():
            return None
        from mlbb_fight_segment import detect_fight_bounds

        start, end, dur = detect_fight_bounds(vod, peak_sec)
        return start, end, dur, {"anchor": "motion", "banner_sec": peak_sec}

    start, end, dur = bounds_from_banner(hit.sec, file_dur)
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
        },
    )


def verify_rendered_clip(path: Path, *, min_tier: int | None = None) -> tuple[bool, str]:
    """Presend: streak banner must appear inside rendered mp4."""
    if os.environ.get("MLBB_VOD_KILL_BANNER", "1") != "1":
        return True, "banner_check_off"
    from smart_video_editor import ffprobe_duration

    dur = ffprobe_duration(path)
    if dur < 1.0:
        return False, "clip_too_short"
    need = min_tier if min_tier is not None else _min_tier()
    # Banner should land after lead-in (~10s into clip).
    lead = _banner_lead_sec()
    t0 = max(0.0, lead - 2.0)
    t1 = min(dur, lead + 4.0)
    hits = scan_window(path, t0, t1)
    for hit in hits:
        if hit.tier >= need:
            return True, f"banner_ok:{hit.label}@{hit.sec:.1f}s"
    if hits and not _banner_required():
        return True, f"banner_weak:{hits[0].label}"
    return False, f"banner_missing_min_tier={need}"
