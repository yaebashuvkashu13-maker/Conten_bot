#!/usr/bin/env python3
"""Soften VOD gates after consecutive zero-cut VODs — avoid wasting whole days."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator

# After N consecutive VODs with sent=0, next VOD runs with softer env overrides.
DEFAULT_STREAK_THRESHOLD = 3

# Level 1: lower score/uniform thresholds only — banner + POV stay mandatory.
SOFTEN_L1: dict[str, str] = {
    "MLBB_VOD_LENIENT_UNIFORM": "1",
    "MLBB_VOD_TAIL_MIN_HUD_RATE": "0.40",
    "SMART_UNIFORM_MIN_HUD_RATE": "0.55",
    "MLBB_PRESEND_MIN_MOTION": "0.014",
    "MLBB_VOD_MIN_CLIP_SCORE": "0.06",
    "VIRAL_MLBB_HOOK_MIN": "0.04",
}

# Level 2: more peak tries + relaxed presend motion — still require verified double+ banner.
SOFTEN_L2: dict[str, str] = {
    **SOFTEN_L1,
    "MLBB_PRESEND_MIN_MOTION": "0.012",
    "MLBB_PRESEND_MIN_MINIMAP_DELTA": "0.010",
    "MLBB_VOD_MIN_CLIP_SCORE": "0.05",
    "HIGHLIGHT_MLBB_AUTO_CLIP_MIN": "0.08",
    "MLBB_VOD_TAIL_MIN_HUD_RATE": "0.38",
    "MLBB_VOD_RESERVED_SENT_ONLY": "1",
    "MLBB_VOD_SOFT_SEGMENT_GAP_SEC": "28",
}


def soft_max_peak_tries() -> int:
    return max(1, int(os.environ.get("MLBB_VOD_SOFT_MAX_PEAK_TRIES", "8")))


def peak_near_skipped(peak: float, skip_peaks: set[float], *, tol: float = 4.0) -> bool:
    return any(abs(peak - s) <= tol for s in skip_peaks)


def streak_threshold() -> int:
    return max(1, int(os.environ.get("MLBB_VOD_ZERO_STREAK_SOFTEN", str(DEFAULT_STREAK_THRESHOLD))))


def soften_level(streak: int) -> int:
    """0=strict, 1=soft (streak>=threshold), 2=softer (streak>=threshold+3)."""
    if os.environ.get("MLBB_VOD_DISABLE_SOFTEN", "0") == "1":
        return 0
    need = streak_threshold()
    if streak < need:
        return 0
    if streak >= need + 3:
        return 2
    return 1


def overrides_for_level(level: int) -> dict[str, str]:
    if level <= 0:
        return {}
    if level >= 2:
        return dict(SOFTEN_L2)
    return dict(SOFTEN_L1)


def trailing_zero_streak(results: list[dict]) -> int:
    n = 0
    for row in reversed(results):
        if int(row.get("sent", 0)) > 0:
            break
        n += 1
    return n


def record_vod_outcome(state: dict, *, vod_id: str, sent: int) -> int:
    """Append outcome, return new trailing zero streak."""
    hist = list(state.get("vod_outcomes") or [])
    hist.append({"id": vod_id, "sent": int(sent)})
    state["vod_outcomes"] = hist[-40:]
    streak = trailing_zero_streak(state["vod_outcomes"])
    state["zero_cut_streak"] = streak
    return streak


def streak_from_state(state: dict) -> int:
    from_hist = 0
    hist = state.get("vod_outcomes")
    if isinstance(hist, list) and hist:
        from_hist = trailing_zero_streak(hist)
    legacy = int(state.get("zero_cut_streak") or 0)
    return max(from_hist, legacy)


def streak_circuit_max() -> int:
    """After this many consecutive zero-VOD runs, reset soften state (avoid infinite L2 loop)."""
    return max(6, int(os.environ.get("MLBB_VOD_STREAK_CIRCUIT_MAX", "12")))


def apply_circuit_breaker(state: dict) -> bool:
    """Reset adaptive streak when stuck too long. Returns True if reset happened."""
    streak = streak_from_state(state)
    if streak < streak_circuit_max():
        return False
    state["zero_cut_streak"] = 0
    state["vod_outcomes"] = []
    state["last_adaptive_level"] = 0
    state["circuit_breaker_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return True


def soften_summary(level: int) -> str:
    if level <= 0:
        return "strict"
    ov = overrides_for_level(level)
    min_clip = ov.get("MLBB_VOD_MIN_CLIP_SCORE", "?")
    motion = ov.get("MLBB_PRESEND_MIN_MOTION", "?")
    return f"soft L{level} clip>={min_clip} motion>={motion} banner=required"


def should_notify_soften(streak: int, level: int, *, prev_level: int) -> bool:
    """Notify only on strict→L1 or L1→L2 transition, not every VOD."""
    if level <= 0:
        return False
    if level > prev_level:
        return True
    need = streak_threshold()
    if level == 1 and streak == need and prev_level == 0:
        return True
    if level == 2 and streak == need + 3 and prev_level < 2:
        return True
    return False


@contextmanager
def adaptive_env(streak: int) -> Iterator[int]:
    """Apply soften overrides for this VOD scan; yields active soften level."""
    level = soften_level(streak)
    overrides = overrides_for_level(level)
    if not overrides:
        yield 0
        return
    saved = {k: os.environ.get(k) for k in overrides}
    saved_level = os.environ.get("MLBB_VOD_ADAPTIVE_LEVEL")
    try:
        os.environ.update(overrides)
        os.environ["MLBB_VOD_ADAPTIVE_LEVEL"] = str(level)
        yield level
    finally:
        for key, prev in saved.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        if saved_level is None:
            os.environ.pop("MLBB_VOD_ADAPTIVE_LEVEL", None)
        else:
            os.environ["MLBB_VOD_ADAPTIVE_LEVEL"] = saved_level


def telegram_soften_notice(streak: int, level: int) -> str:
    return (
        f"⚙️ Серия без клипов: {streak}. Включаю {soften_summary(level)}.\n"
        f"Double Kill + POV героя остаются обязательными; смягчаются только score/motion."
    )


def telegram_exhaust_notice(vod_id: str, *, level: int, streak: int) -> str:
    base = f"⚠️ {vod_id}: 0 клипов"
    if level > 0:
        return f"{base} (уже мягкий режим L{level}, серия нулей={streak})"
    if os.environ.get("MLBB_VOD_DISABLE_SOFTEN", "0") == "1":
        return f"{base} — серия нулей={streak} (смягчение отключено: quality mode)"
    return f"{base} — серия нулей {streak}/{streak_threshold()} до смягчения"
