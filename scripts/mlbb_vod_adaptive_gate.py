#!/usr/bin/env python3
"""Soften VOD gates after consecutive zero-cut VODs — avoid wasting whole days."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

# After N consecutive VODs with sent=0, next VOD runs with softer env overrides.
DEFAULT_STREAK_THRESHOLD = 3

# Level 1: after silence — keep kill-banner required (owner: no empty running).
# Only widen OCR windows and slightly relax clip score.
SOFTEN_L1: dict[str, str] = {
    "MLBB_KILL_BANNER_MIN_TIER": "double",
    "MLBB_KILL_BANNER_REQUIRED": "1",
    "MLBB_VOD_BANNER_PRESEND": "1",
    "MLBB_VOD_BANNER_DISCOVER": "1",
    "MLBB_VOD_MOTION_ANCHOR_OK": "0",
    "MLBB_VOD_BANNER_SKIP_ON_MISS": "0",
    "MLBB_VOD_LENIENT_UNIFORM": "1",
    "MLBB_VOD_TAIL_MIN_HUD_RATE": "0.42",
    "SMART_UNIFORM_MIN_HUD_RATE": "0.55",
    "MLBB_PRESEND_MIN_MOTION": "0.016",
    "MLBB_VOD_MIN_CLIP_SCORE": "0.10",
    "HIGHLIGHT_MLBB_AUTO_CLIP_MIN": "0.12",
    "VIRAL_MLBB_HOOK_MIN": "0.08",
    "VIRAL_MLBB_CLIP_HOOK_MIN": "0.18",
    "MLBB_RULE_COMBAT_MIN": "0.82",
    "MLBB_TEAMFIGHT_MIN_SCORE": "0.42",
    "MLBB_KILL_BANNER_QUICK_BEFORE": "16",
    "MLBB_KILL_BANNER_QUICK_AFTER": "10",
}

# Level 2: still prefer banner; allow single; do NOT hard-skip whole VOD on miss.
SOFTEN_L2: dict[str, str] = {
    **SOFTEN_L1,
    "MLBB_KILL_BANNER_MIN_TIER": "single",
    "MLBB_PRESEND_MIN_MOTION": "0.014",
    "MLBB_PRESEND_MIN_MINIMAP_DELTA": "0.010",
    "MLBB_VOD_MIN_CLIP_SCORE": "0.09",
    "HIGHLIGHT_MLBB_AUTO_CLIP_MIN": "0.11",
    "MLBB_VOD_TAIL_MIN_HUD_RATE": "0.40",
    "MLBB_KILL_BANNER_QUICK_BEFORE": "20",
    "MLBB_KILL_BANNER_QUICK_AFTER": "12",
    "MLBB_KILL_BANNER_SCAN_BEFORE": "28",
    "MLBB_KILL_BANNER_SCAN_AFTER": "14",
    "MLBB_VOD_RESERVED_SENT_ONLY": "1",
    "MLBB_VOD_SOFT_SEGMENT_GAP_SEC": "28",
    # Prefer banner; allow single; never hard-skip the whole VOD on OCR miss.
    "MLBB_VOD_BANNER_HARD_PREFILTER": "0",
    "MLBB_VOD_BANNER_SKIP_ON_MISS": "0",
    "MLBB_RULE_COMBAT_MIN": "0.80",
    "MLBB_TEAMFIGHT_MIN_SCORE": "0.40",
}

# Level 3: long silence — ship strong teamfights if banner OCR is blind.
# Motion anchors OK only with high combat score — not farming/recall junk.
SOFTEN_L3: dict[str, str] = {
    **SOFTEN_L2,
    "MLBB_KILL_BANNER_REQUIRED": "0",
    "MLBB_VOD_MOTION_ANCHOR_OK": "1",
    "MLBB_VOD_BANNER_PRESEND": "0",
    "MLBB_VOD_MIN_CLIP_SCORE": "0.12",
    "HIGHLIGHT_MLBB_AUTO_CLIP_MIN": "0.14",
    "MLBB_PRESEND_MIN_MOTION": "0.018",
    "MLBB_RULE_COMBAT_MIN": "0.88",
    "MLBB_TEAMFIGHT_MIN_SCORE": "0.45",
    "MLBB_TEAMFIGHT_RANK_FRAC": "0.85",
    "MLBB_MOTION_PEAK_MAX": "3",
    "VIRAL_MLBB_HOOK_MIN": "0.10",
    "VIRAL_MLBB_CLIP_HOOK_MIN": "0.20",
    # Soften can find weak motion peaks — never stitch them into a montage.
    "MLBB_VOD_MONTAGE": "0",
    "MLBB_VOD_SEND_ONE": "1",
}


def soft_max_peak_tries() -> int:
    return max(1, int(os.environ.get("MLBB_VOD_SOFT_MAX_PEAK_TRIES", "8")))


def peak_near_skipped(peak: float, skip_peaks: set[float], *, tol: float = 4.0) -> bool:
    return any(abs(peak - s) <= tol for s in skip_peaks)


def streak_threshold() -> int:
    return max(1, int(os.environ.get("MLBB_VOD_ZERO_STREAK_SOFTEN", str(DEFAULT_STREAK_THRESHOLD))))


def soften_level(streak: int) -> int:
    """0=strict, 1=soft, 2=softer, 3=ship-motion (long silence)."""
    need = streak_threshold()
    if streak < need:
        return 0
    if streak >= need + 4:
        return 3
    if streak >= need + 3:
        return 2
    return 1


def overrides_for_level(level: int) -> dict[str, str]:
    if level <= 0:
        return {}
    if level >= 3:
        return dict(SOFTEN_L3)
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


def soften_summary(level: int) -> str:
    if level <= 0:
        return "strict"
    ov = overrides_for_level(level)
    tier = ov.get("MLBB_KILL_BANNER_MIN_TIER", "?")
    pre = "off" if ov.get("MLBB_VOD_BANNER_PREFILTER") == "0" else "on"
    anchor = "motion_ok" if ov.get("MLBB_KILL_BANNER_REQUIRED") == "0" else "banner_required"
    return f"soft L{level} tier={tier} prefilter={pre} {anchor}"


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
        f"Kill-banner обязателен; мягче только OCR-окна и score."
    )


def telegram_exhaust_notice(vod_id: str, *, level: int, streak: int) -> str:
    base = f"⚠️ {vod_id}: 0 клипов"
    if level > 0:
        return f"{base} (уже мягкий режим L{level}, серия нулей={streak})"
    return f"{base} — серия нулей {streak}/{streak_threshold()} до смягчения"
