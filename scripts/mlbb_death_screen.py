#!/usr/bin/env python3
"""MLBB death / respawn timer screen — trim idle tail from VOD clips."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import logging

log = logging.getLogger("mlbb_death_screen")

_DEATH_TEXT_RE = re.compile(
    r"(?:"
    r"respawn|revive|resurrect|reborn|you\s+died|has\s+been\s+slain|defeated|"
    r"eliminated|slain|killed|death\s+timer|wait\s+to\s+respawn"
    r"|умер|погиб|убит|смерт|воскрешен|возрожден|оживлен|ожидани.{0,12}возрожд"
    r"|таймер.{0,8}возрожд|секунд.{0,8}до\s+возрожд"
    r")",
    re.I,
)

_COUNTDOWN_RE = re.compile(r"(?:respawn|revive|возрожд|ожидани)[^\d]{0,20}(\d{1,2})", re.I)


@dataclass(frozen=True)
class DeathScreenHit:
    sec: float
    text: str
    source: str
    timer_sec: float | None = None


def death_trim_enabled() -> bool:
    return os.environ.get("MLBB_VOD_DEATH_TRIM", "1") == "1"


def _scan_step() -> float:
    return float(os.environ.get("MLBB_DEATH_SCAN_STEP", "0.45"))


def _timer_max_sec() -> float:
    return float(os.environ.get("MLBB_DEATH_TIMER_MAX_SEC", "16"))


def _bottom_text_min() -> float:
    return float(os.environ.get("MLBB_DEATH_BOTTOM_TEXT_MIN", "0.11"))


def _min_clip_after_trim() -> float:
    return float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7"))


def classify_death_text(text: str) -> bool:
    blob = " ".join(str(text or "").split())
    if not blob:
        return False
    return bool(_DEATH_TEXT_RE.search(blob))


def parse_respawn_countdown(text: str) -> float | None:
    blob = " ".join(str(text or "").split())
    if not blob:
        return None
    m = _COUNTDOWN_RE.search(blob)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    nums = [int(x) for x in re.findall(r"\b(\d{1,2})\b", blob) if 1 <= int(x) <= 20]
    if nums and classify_death_text(blob):
        return float(max(nums))
    return None


def _ocr_death_zone(frame) -> str:
    import cv2

    try:
        import pytesseract
    except ImportError:
        return ""

    small = cv2.resize(frame, (480, 270))
    h, w = small.shape[:2]
    zones = [
        small[int(h * 0.70) : int(h * 0.96), int(w * 0.22) : int(w * 0.78)],
        small[int(h * 0.74) : int(h * 0.94), int(w * 0.30) : int(w * 0.70)],
    ]
    texts: list[str] = []
    for zone in zones:
        if zone.size == 0:
            continue
        gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
        for variant in (
            cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
            cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1],
        ):
            for psm in (7, 8):
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
                    if classify_death_text(text):
                        return " ".join(texts)
    return " ".join(texts)


def _bottom_band_text_score(frame) -> float:
    from gameplay_gate import _band_overlay_text_score

    return _band_overlay_text_score(frame, 0.72, 0.94)


def death_frame_score(vod: Path, sec: float) -> tuple[float, str]:
    """
    Score 0..1 — higher = more likely death/respawn screen.
    Returns (score, hint).
    """
    from gameplay_gate import _frame_hud_metrics, _read_frame_at

    frame = _read_frame_at(vod, sec)
    if frame is None:
        return 0.0, "no_frame"

    bottom = _bottom_band_text_score(frame)
    mini, skill, _top = _frame_hud_metrics(frame)
    ocr = _ocr_death_zone(frame)
    if classify_death_text(ocr):
        return 1.0, f"ocr:{ocr[:48]}"

    # Death UI: prominent bottom-center caption, skills frozen/greyed (low std).
    skill_dead = skill < float(os.environ.get("MLBB_DEATH_MAX_SKILL_STD", "5.5"))
    bottom_strong = bottom >= _bottom_text_min()
    if bottom_strong and skill_dead:
        return min(1.0, 0.55 + bottom * 2.0), f"heuristic:bottom={bottom:.3f}:skill={skill:.1f}"
    if bottom_strong and mini < float(os.environ.get("MLBB_DEATH_MAX_MINI_STD", "6.0")):
        return min(1.0, 0.50 + bottom * 1.8), f"heuristic:bottom={bottom:.3f}:mini={mini:.1f}"
    return max(0.0, bottom * 0.6), "weak"


def probe_death_at(vod: Path, sec: float) -> DeathScreenHit | None:
    score, hint = death_frame_score(vod, sec)
    if score < float(os.environ.get("MLBB_DEATH_SCORE_MIN", "0.62")):
        return None
    ocr = hint[4:] if hint.startswith("ocr:") else ""
    if not ocr:
        frame = None
        from gameplay_gate import _read_frame_at

        frame = _read_frame_at(vod, sec)
        if frame is not None:
            ocr = _ocr_death_zone(frame)
    timer = parse_respawn_countdown(ocr) if ocr else None
    return DeathScreenHit(sec=sec, text=ocr or hint, source="ocr" if ocr else "heuristic", timer_sec=timer)


def find_death_start_in_window(vod: Path, start_sec: float, end_sec: float) -> DeathScreenHit | None:
    """First death/respawn screen inside [start_sec, end_sec)."""
    if end_sec <= start_sec + 1.0:
        return None
    step = _scan_step()
    t = max(0.0, float(start_sec))
    end = float(end_sec)
    while t < end - 0.35:
        hit = probe_death_at(vod, t)
        if hit is not None:
            return hit
        t += step
    return None


def death_window_end(vod: Path, death_start: float, file_dur: float, timer_sec: float | None) -> float:
    """End of respawn wait — scan until HUD returns or timer elapses."""
    cap = min(_timer_max_sec(), (timer_sec or 12.0) + 1.5)
    step = _scan_step()
    t = float(death_start) + step
    limit = min(float(file_dur), float(death_start) + cap)
    while t < limit:
        score, _ = death_frame_score(vod, t)
        if score < float(os.environ.get("MLBB_DEATH_SCORE_MIN", "0.62")) * 0.85:
            return t
        t += step
    return limit


def trim_death_tail(
    vod: Path,
    start_sec: float,
    end_sec: float,
    *,
    file_dur: float | None = None,
) -> tuple[float, float, dict]:
    """
    Cut clip before death/respawn timer UI.
    Returns (start, end, meta). Meta empty when unchanged.
    """
    if not death_trim_enabled():
        return start_sec, end_sec, {}
    if file_dur is None:
        from smart_video_editor import ffprobe_duration

        file_dur = ffprobe_duration(vod)
    hit = find_death_start_in_window(vod, start_sec, end_sec)
    if hit is None:
        return start_sec, end_sec, {}
    death_end = death_window_end(vod, hit.sec, file_dur, hit.timer_sec)
    new_end = max(float(start_sec) + _min_clip_after_trim(), float(hit.sec))
    if new_end >= end_sec - 0.4:
        return start_sec, end_sec, {}
    meta = {
        "death_trim": True,
        "death_start": round(hit.sec, 2),
        "death_end": round(death_end, 2),
        "death_text": hit.text[:80],
        "death_source": hit.source,
    }
    if hit.timer_sec is not None:
        meta["death_timer_sec"] = hit.timer_sec
    log.info(
        "death trim %s: %.1f→%.1f (death@%.1fs timer=%s)",
        vod.name,
        end_sec,
        new_end,
        hit.sec,
        hit.timer_sec,
    )
    return start_sec, round(new_end, 2), meta


def segment_death_ratio(
    vod: Path,
    start_sec: float,
    duration_sec: float,
    *,
    sample_frames: int = 6,
) -> float:
    """Share of sampled frames that look like death/respawn UI."""
    if duration_sec <= 0:
        return 0.0
    import numpy as np

    end = start_sec + duration_sec
    times = np.linspace(start_sec, max(start_sec + 0.1, end - 0.05), num=sample_frames)
    hits = 0
    total = 0
    min_score = float(os.environ.get("MLBB_DEATH_SCORE_MIN", "0.62"))
    for t in times:
        score, _ = death_frame_score(vod, float(t))
        total += 1
        if score >= min_score:
            hits += 1
    return hits / max(total, 1)


def segment_mostly_death_screen(vod: Path, start_sec: float, duration_sec: float) -> bool:
    ratio = segment_death_ratio(vod, start_sec, duration_sec)
    return ratio >= float(os.environ.get("MLBB_DEATH_REJECT_RATIO", "0.45"))


def peak_inside_death_window(vod: Path, peak_sec: float, *, pad_sec: float = 2.0) -> bool:
    """Skip highlight peaks that land on respawn timer."""
    if not death_trim_enabled():
        return False
    win = max(4.0, float(os.environ.get("MLBB_DEATH_PEAK_WINDOW_SEC", "8")))
    hit = find_death_start_in_window(vod, max(0.0, peak_sec - pad_sec), peak_sec + win)
    if hit is None:
        return False
    from smart_video_editor import ffprobe_duration

    end = death_window_end(vod, hit.sec, ffprobe_duration(vod), hit.timer_sec)
    return hit.sec - pad_sec <= peak_sec <= end
