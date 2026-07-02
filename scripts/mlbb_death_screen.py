#!/usr/bin/env python3
"""MLBB death / respawn timer screen — trim idle tail from VOD clips."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import logging

log = logging.getLogger("mlbb_death_screen")

# MLBB death UI (RU/EN client): gray spectator overlay, large countdown mid-screen,
# death caption bottom-center («Вы погибли» / respawn text).
_DEATH_TEXT_RE = re.compile(
    r"(?:"
    r"respawn|revive|resurrect|reborn|you\s+died|has\s+been\s+slain|defeated|"
    r"eliminated|slain|killed|killed\s+by|death\s+timer|wait\s+to\s+respawn|"
    r"reborn\s+in|respawning"
    r"|умер|погиб|убит|убиты|смерт|воскрешен|возрожден|оживлен|"
    r"ожидани.{0,16}возрожд|возрождени.{0,12}через|перерожден"
    r"|таймер.{0,8}возрожд|секунд.{0,12}(?:до\s+)?возрожд|"
    r"вы\s+(?:были\s+)?(?:убит|погиб)"
    r")",
    re.I,
)

_COUNTDOWN_RE = re.compile(
    r"(?:respawn|revive|reborn|возрожд|ожидани|через)[^\d]{0,24}(\d{1,2})",
    re.I,
)

_SEC_COUNTDOWN_RE = re.compile(r"(\d{1,2})\s*(?:сек|sec|s\b)", re.I)


@dataclass(frozen=True)
class DeathScreenHit:
    sec: float
    text: str
    source: str
    timer_sec: float | None = None


def death_trim_enabled() -> bool:
    return os.environ.get("MLBB_VOD_DEATH_TRIM", "1") == "1"


def _scan_step() -> float:
    return float(os.environ.get("MLBB_DEATH_SCAN_STEP", "1.2"))


def _ocr_enabled() -> bool:
    return os.environ.get("MLBB_DEATH_USE_OCR", "0") == "1"


def _tail_scan_sec() -> float:
    return float(os.environ.get("MLBB_DEATH_TAIL_SCAN_SEC", "14"))


def _max_probes() -> int:
    return max(3, int(os.environ.get("MLBB_DEATH_MAX_PROBES", "8")))


def _timer_max_sec() -> float:
    return float(os.environ.get("MLBB_DEATH_TIMER_MAX_SEC", "55"))


def _bottom_text_min() -> float:
    return float(os.environ.get("MLBB_DEATH_BOTTOM_TEXT_MIN", "0.09"))


def _min_clip_after_trim() -> float:
    return float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7"))


def _post_death_keep_sec() -> float:
    """Seconds of death screen to keep after a fight moment (before long respawn wait)."""
    return float(os.environ.get("MLBB_DEATH_POST_KEEP_SEC", "4"))


def death_trim_end(start_sec: float, end_sec: float, death_start_sec: float) -> float | None:
    """
    New clip end: fight + up to post_keep sec after death onset.
    Returns None when trim would not shorten the clip.
    """
    keep = _post_death_keep_sec()
    min_end = float(start_sec) + _min_clip_after_trim()
    new_end = max(min_end, float(death_start_sec) + keep)
    new_end = min(new_end, float(end_sec))
    if new_end >= float(end_sec) - 0.4:
        return None
    return round(new_end, 2)


def classify_death_text(text: str) -> bool:
    blob = " ".join(str(text or "").split())
    if not blob:
        return False
    return bool(_DEATH_TEXT_RE.search(blob))


def parse_respawn_countdown(text: str, *, digit_hint: float | None = None) -> float | None:
    if digit_hint is not None and 1 <= digit_hint <= 60:
        return float(digit_hint)
    blob = " ".join(str(text or "").split())
    if not blob:
        return None
    m = _COUNTDOWN_RE.search(blob)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m2 = _SEC_COUNTDOWN_RE.search(blob)
    if m2:
        try:
            return float(m2.group(1))
        except ValueError:
            pass
    nums = [int(x) for x in re.findall(r"\b(\d{1,2})\b", blob) if 1 <= int(x) <= 60]
    if nums and (classify_death_text(blob) or len(blob) <= 8):
        return float(max(nums))
    return None


def _frame_zones(frame):
    """Return resized frame + zone slices: caption (bottom-center), timer (mid-center)."""
    import cv2

    small = cv2.resize(frame, (480, 270))
    h, w = small.shape[:2]
    caption = small[int(h * 0.80) : int(h * 0.98), int(w * 0.30) : int(w * 0.70)]
    timer = small[int(h * 0.48) : int(h * 0.76), int(w * 0.36) : int(w * 0.64)]
    wide_caption = small[int(h * 0.74) : int(h * 0.96), int(w * 0.22) : int(w * 0.78)]
    return small, caption, timer, wide_caption


def _ocr_zone_text(zone, *, digits_only: bool = False) -> str:
    import cv2
    import subprocess
    import tempfile

    if zone.size == 0:
        return ""
    if not _ocr_enabled():
        return ""

    gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
    variant = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    whitelist = "0123456789" if digits_only else ""
    psm = "10" if digits_only else "7"
    timeout = float(os.environ.get("MLBB_DEATH_OCR_TIMEOUT_SEC", "2.5"))

    try:
        import pytesseract
    except ImportError:
        return ""

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        import cv2 as _cv2

        _cv2.imwrite(tmp_path, variant)
        proc = subprocess.run(
            ["tesseract", tmp_path, "stdout", "-l", "eng+rus", "--psm", psm]
            + (["-c", f"tessedit_char_whitelist={whitelist}"] if whitelist else []),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return " ".join((proc.stdout or "").split())
    except (subprocess.TimeoutExpired, OSError):
        return ""
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass


def _ocr_death_caption(frame) -> str:
    _small, caption, _timer, wide = _frame_zones(frame)
    blob = " ".join(filter(None, (_ocr_zone_text(caption), _ocr_zone_text(wide))))
    if classify_death_text(blob):
        return blob
    return blob


def _ocr_timer_digits(frame) -> tuple[str, float | None]:
    _small, _caption, timer, _wide = _frame_zones(frame)
    raw = _ocr_zone_text(timer, digits_only=True)
    raw = raw.replace("O", "0").replace("o", "0").replace("l", "1")
    nums = [int(x) for x in re.findall(r"\d{1,2}", raw) if 1 <= int(x) <= 60]
    if not nums:
        text = _ocr_zone_text(timer, digits_only=False)
        nums = [int(x) for x in re.findall(r"\b(\d{1,2})\b", text) if 1 <= int(x) <= 60]
        raw = text or raw
    if nums:
        return raw, float(nums[0])
    return raw, None


def _ocr_death_zone(frame) -> str:
    caption = _ocr_death_caption(frame)
    timer_raw, timer_val = _ocr_timer_digits(frame)
    parts = [caption, timer_raw]
    blob = " ".join(p for p in parts if p)
    if classify_death_text(blob) or timer_val is not None:
        return blob
    return blob


def _death_overlay_score(frame) -> float:
    """Gray spectator overlay — arena saturation drops on MLBB death screen."""
    import cv2
    import numpy as np

    small = cv2.resize(frame, (320, 180))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    h, w = small.shape[:2]
    arena = hsv[int(h * 0.12) : int(h * 0.62), int(w * 0.12) : int(w * 0.88)]
    if arena.size == 0:
        return 0.0
    mean_sat = float(np.mean(arena[:, :, 1]))
    # Typical death overlay: S mean ~20-45 vs live fight ~55-90
    if mean_sat <= 38:
        return min(1.0, (48 - mean_sat) / 22.0)
    if mean_sat <= 52:
        return max(0.0, (52 - mean_sat) / 28.0)
    return 0.0


def _bottom_caption_text_score(frame) -> float:
    from gameplay_gate import _band_overlay_text_score

    # Tight bottom-center band where «умер» / respawn caption sits.
    return _band_overlay_text_score(frame, 0.80, 0.97)


def death_frame_score(vod: Path, sec: float) -> tuple[float, str]:
    """
    Score 0..1 — higher = more likely death/respawn screen.
    Fast path: heuristics only unless MLBB_DEATH_USE_OCR=1.
    """
    from gameplay_gate import _frame_hud_metrics, _read_frame_at

    frame = _read_frame_at(vod, sec)
    if frame is None:
        return 0.0, "no_frame"

    caption_bottom = _bottom_caption_text_score(frame)
    overlay = _death_overlay_score(frame)
    mini, skill, _top = _frame_hud_metrics(frame)
    skill_dead = skill < float(os.environ.get("MLBB_DEATH_MAX_SKILL_STD", "5.5"))
    caption_strong = caption_bottom >= _bottom_text_min()

    heuristic = 0.0
    hint = "weak"
    if overlay >= 0.42 and caption_strong and skill_dead:
        heuristic = min(1.0, 0.72 + overlay * 0.2)
        hint = f"overlay_caption:ov={overlay:.2f}"
    elif caption_strong and skill_dead:
        heuristic = min(1.0, 0.58 + caption_bottom * 1.6)
        hint = f"caption_skill:bottom={caption_bottom:.3f}"
    elif caption_strong and mini < float(os.environ.get("MLBB_DEATH_MAX_MINI_STD", "6.0")):
        heuristic = min(1.0, 0.52 + caption_bottom * 1.4)
        hint = f"caption_mini:bottom={caption_bottom:.3f}"
    elif overlay >= 0.35 and caption_strong:
        heuristic = 0.55
        hint = f"overlay_bottom:ov={overlay:.2f}"

    min_score = float(os.environ.get("MLBB_DEATH_SCORE_MIN", "0.58"))
    if heuristic >= min_score:
        return heuristic, hint

    if not _ocr_enabled() or heuristic < 0.35:
        return max(0.0, heuristic), hint

    ocr_caption = _ocr_death_caption(frame)
    if classify_death_text(ocr_caption):
        return 1.0, f"ocr_caption:{ocr_caption[:48]}"

    timer_raw, timer_val = _ocr_timer_digits(frame)
    if timer_val is not None and overlay >= 0.28 and (caption_strong or skill_dead):
        return (
            min(1.0, 0.88 + overlay * 0.1),
            f"timer_digit:{int(timer_val)}:overlay={overlay:.2f}",
        )
    if timer_raw and timer_val is not None and overlay >= 0.22:
        return 0.65, f"timer_weak:{int(timer_val)}"

    return max(heuristic, caption_bottom * 0.5), hint


def probe_death_at(vod: Path, sec: float) -> DeathScreenHit | None:
    score, hint = death_frame_score(vod, sec)
    if score < float(os.environ.get("MLBB_DEATH_SCORE_MIN", "0.58")):
        return None

    from gameplay_gate import _read_frame_at

    frame = _read_frame_at(vod, sec)
    ocr = ""
    timer_val = None
    if frame is not None:
        ocr = _ocr_death_caption(frame)
        _raw, timer_val = _ocr_timer_digits(frame)
        if not ocr:
            ocr = _raw
    if hint.startswith("ocr_caption:"):
        ocr = hint.split(":", 1)[1]
    elif hint.startswith("timer_digit:"):
        try:
            timer_val = float(hint.split(":")[1].split(":")[0])
        except ValueError:
            pass

    timer = parse_respawn_countdown(ocr, digit_hint=timer_val)
    source = "ocr" if classify_death_text(ocr) else ("timer_digit" if timer_val else "heuristic")
    return DeathScreenHit(sec=sec, text=ocr or hint, source=source, timer_sec=timer)


def find_death_start_in_window(vod: Path, start_sec: float, end_sec: float) -> DeathScreenHit | None:
    """First death/respawn screen inside clip tail (cheap backward scan)."""
    if end_sec <= start_sec + 1.0:
        return None
    step = _scan_step()
    tail = min(_tail_scan_sec(), max(0.0, end_sec - start_sec))
    t = max(float(start_sec), float(end_sec) - tail)
    end = float(end_sec)
    probes = 0
    while t < end - 0.35 and probes < _max_probes():
        hit = probe_death_at(vod, t)
        if hit is not None:
            return hit
        t += step
        probes += 1
    return None


def death_window_end(vod: Path, death_start: float, file_dur: float, timer_sec: float | None) -> float:
    """End of respawn wait — skip for full countdown length."""
    cap = min(_timer_max_sec(), (timer_sec or 14.0) + 2.0)
    step = _scan_step()
    t = float(death_start) + step
    limit = min(float(file_dur), float(death_start) + cap)
    min_score = float(os.environ.get("MLBB_DEATH_SCORE_MIN", "0.58"))
    probes = 0
    while t < limit and probes < _max_probes():
        score, _ = death_frame_score(vod, t)
        if score < min_score * 0.82:
            return t
        t += step
        probes += 1
    return limit


def trim_death_tail(
    vod: Path,
    start_sec: float,
    end_sec: float,
    *,
    file_dur: float | None = None,
) -> tuple[float, float, dict]:
    """
    Cut long respawn wait after death; keep a short post-death tail (default 4s).
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
    new_end = death_trim_end(start_sec, end_sec, hit.sec)
    if new_end is None:
        return start_sec, end_sec, {}
    meta = {
        "death_trim": True,
        "death_start": round(hit.sec, 2),
        "death_end": round(death_end, 2),
        "death_post_keep_sec": _post_death_keep_sec(),
        "death_text": hit.text[:80],
        "death_source": hit.source,
    }
    if hit.timer_sec is not None:
        meta["death_timer_sec"] = hit.timer_sec
    log.info(
        "death trim %s: %.1f→%.1f (death@%.1fs keep=%.0fs timer=%ss)",
        vod.name,
        end_sec,
        new_end,
        hit.sec,
        _post_death_keep_sec(),
        hit.timer_sec,
    )
    return start_sec, new_end, meta


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
    min_score = float(os.environ.get("MLBB_DEATH_SCORE_MIN", "0.58"))
    for t in times:
        score, _ = death_frame_score(vod, float(t))
        total += 1
        if score >= min_score:
            hits += 1
    return hits / max(total, 1)


def segment_mostly_death_screen(vod: Path, start_sec: float, duration_sec: float) -> bool:
    keep = _post_death_keep_sec()
    check_dur = duration_sec
    if duration_sec > keep + 1.0:
        check_dur = max(1.0, duration_sec - keep)
    ratio = segment_death_ratio(vod, start_sec, check_dur)
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
