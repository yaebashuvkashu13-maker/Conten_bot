#!/usr/bin/env python3
"""Author-kill quality for shooter montages (PUBG / Standoff / WoT).

Owner rule: never ship clips where the POV author mostly dies / gets finished.
If the author got a kill earlier in the window, that fight is OK — death-only
and spectator-of-own-death trash is not.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import numpy as np

DEATH_OCR_PATTERNS = (
    re.compile(r"killed\s+by", re.I),
    re.compile(r"you\s+were\s+killed", re.I),
    re.compile(r"you\s+died", re.I),
    re.compile(r"\bkilled\s+you\b", re.I),
    re.compile(r"\brespawn\b", re.I),
    re.compile(r"\bspectat", re.I),
    re.compile(r"убил[аи]?\s+вас", re.I),
    re.compile(r"вас\s+убил", re.I),
    re.compile(r"\bубит[ыа]?\b", re.I),
    re.compile(r"возрожд", re.I),
)

KILL_OCR_PATTERNS = (
    re.compile(r"\bheadshot\b", re.I),
    re.compile(r"\beliminated\b", re.I),
    re.compile(r"\bknock(?:ed|out)?\b", re.I),
    re.compile(r"\bkill\b", re.I),
    re.compile(r"убил", re.I),
    re.compile(r"убийств", re.I),
    re.compile(r"\bnok\b|\bнок\b", re.I),
)


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default) == "1"


def _read_frame(video_path: Path, t: float):
    from gameplay_gate import _read_frame_at

    return _read_frame_at(video_path, t)


def _crop_viewport(frame, crop):
    if frame is None or crop is None:
        return frame
    x, y, w, h = crop
    return frame[y : y + h, x : x + w]


def _ocr_blob(frame, *, y0: float, y1: float, x0: float, x1: float) -> str:
    import cv2

    try:
        import pytesseract
    except ImportError:
        return ""
    if frame is None or getattr(frame, "size", 0) == 0:
        return ""
    small = cv2.resize(frame, (320, 180))
    h, w = small.shape[:2]
    zone = small[int(h * y0) : int(h * y1), int(w * x0) : int(w * x1)]
    if zone.size == 0:
        return ""
    gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
    gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    text = pytesseract.image_to_string(
        gray,
        config="--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+- ",
    )
    return " ".join(text.split())


def _frame_hud_and_tone(frame) -> dict[str, float]:
    import cv2

    if frame is None or getattr(frame, "size", 0) == 0:
        return {"hud": 0.0, "mean": 0.0, "center_std": 0.0, "edge": 0.0}
    small = cv2.resize(frame, (160, 90))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    bottom = gray[int(h * 0.70) : h, :]
    top = gray[0 : int(h * 0.18), :]
    center = gray[int(h * 0.25) : int(h * 0.70), int(w * 0.20) : int(w * 0.80)]
    edges = cv2.Canny(center, 40, 120)
    return {
        "hud": float(np.std(bottom) + np.std(top) * 0.6),
        "mean": float(np.mean(center)),
        "center_std": float(np.std(center)),
        "edge": float(np.mean(edges > 0)),
    }


def _sample_times(start_sec: float, duration_sec: float) -> list[tuple[str, float]]:
    dur = max(1.0, float(duration_sec))
    return [
        ("early", start_sec + dur * 0.18),
        ("mid", start_sec + dur * 0.45),
        ("late", start_sec + dur * 0.72),
        ("tail", start_sec + dur * 0.90),
    ]


def detect_author_death_signals(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[bool, str, dict[str, Any]]:
    """True when the late window looks like author death / killcam / spectate."""
    from gameplay_gate import detect_game_viewport_crop

    crop = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    metrics: dict[str, Any] = {"samples": []}
    ocr_death_hits: list[str] = []
    tones: list[dict[str, float]] = []

    for label, t in _sample_times(start_sec, duration_sec):
        frame = _crop_viewport(_read_frame(video_path, t), crop)
        tone = _frame_hud_and_tone(frame)
        tones.append(tone)
        # Center + top banner OCR for death UI.
        blob = " ".join(
            [
                _ocr_blob(frame, y0=0.28, y1=0.72, x0=0.15, x1=0.85),
                _ocr_blob(frame, y0=0.00, y1=0.22, x0=0.15, x1=0.85),
            ]
        )
        metrics["samples"].append({"label": label, "t": round(t, 2), **tone, "ocr": blob[:80]})
        for pat in DEATH_OCR_PATTERNS:
            if pat.search(blob):
                ocr_death_hits.append(pat.pattern[:28])

    if ocr_death_hits:
        metrics["death_ocr"] = sorted(set(ocr_death_hits))[:4]
        return True, f"author_death_ocr={','.join(metrics['death_ocr'][:2])}", metrics

    if len(tones) < 3:
        return False, "", metrics

    early = tones[0]
    late_vals = tones[-2:]
    late_hud = float(np.mean([x["hud"] for x in late_vals]))
    late_mean = float(np.mean([x["mean"] for x in late_vals]))
    late_edge = float(np.mean([x["edge"] for x in late_vals]))
    early_hud = float(early["hud"])
    early_edge = float(early["edge"])
    metrics.update(
        {
            "early_hud": round(early_hud, 2),
            "late_hud": round(late_hud, 2),
            "late_mean": round(late_mean, 2),
            "late_edge": round(late_edge, 4),
            "early_edge": round(early_edge, 4),
        }
    )

    # Death/killcam: HUD collapses, screen darkens/flattens, edges drop vs early fight.
    hud_collapse = early_hud >= 12.0 and late_hud <= early_hud * 0.45 and late_hud < 9.0
    dark_flat = late_mean <= 55.0 and late_edge <= max(0.035, early_edge * 0.45)
    if hud_collapse and dark_flat:
        return True, f"author_death_hud=late{late_hud:.1f}:early{early_hud:.1f}", metrics
    if late_hud < 6.5 and late_mean <= 48.0 and late_edge < 0.030 and early_hud >= 10.0:
        return True, f"author_death_screen=mean{late_mean:.0f}", metrics
    return False, "", metrics


def detect_author_kill_signals(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    profile: str = "standoff",
    shoot_metrics: dict | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """True when there is evidence the POV author got a kill / decisive hit."""
    from gameplay_gate import detect_game_viewport_crop
    from visual_action_check import check_frame_visual, segment_frame_times

    shoot_metrics = shoot_metrics or {}
    out: dict[str, Any] = {}
    profile = (profile or "standoff").strip().lower()
    if profile not in ("pubg", "standoff", "wot"):
        profile = "standoff"

    # Killfeed OCR (bonus; Standoff OCR is flaky — not sole signal).
    killfeed_density = 0.0
    killfeed_hits: list[str] = []
    try:
        from pubg_killfeed_ocr import score_killfeed_segment

        killfeed_density, kf = score_killfeed_segment(
            video_path, start_sec, duration_sec, profile if profile != "wot" else "pubg"
        )
        killfeed_hits = list(kf.get("killfeed_hits") or [])
        out["killfeed_density"] = round(float(killfeed_density), 3)
        out["killfeed_hits"] = killfeed_hits[:6]
    except Exception:  # noqa: BLE001
        pass

    crop = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    best_flash = 0.0
    best_weapon = 0.0
    ocr_kill = False
    for _label, t in segment_frame_times(start_sec, duration_sec)[:4]:
        frame = _crop_viewport(_read_frame(video_path, t), crop)
        if frame is None:
            continue
        ok, _reason, fmetrics = check_frame_visual(
            "standoff" if profile == "wot" else profile, frame
        )
        best_flash = max(best_flash, float(fmetrics.get("hit_flash", 0) or 0))
        best_weapon = max(best_weapon, float(fmetrics.get("weapon_edge", 0) or 0))
        blob = _ocr_blob(frame, y0=0.02, y1=0.22, x0=0.55, x1=0.99)
        if any(pat.search(blob) for pat in KILL_OCR_PATTERNS):
            ocr_kill = True
        _ = ok

    out["hit_flash"] = round(best_flash, 4)
    out["weapon_edge"] = round(best_weapon, 4)
    out["ocr_kill"] = ocr_kill

    min_flash = float(os.environ.get("SHOOTER_AUTHOR_KILL_MIN_HIT_FLASH", "0.004"))
    min_weapon = float(os.environ.get("SHOOTER_AUTHOR_KILL_MIN_WEAPON_EDGE", "0.030"))
    min_kf = float(os.environ.get("SHOOTER_AUTHOR_KILL_MIN_KILLFEED", "0.30"))
    gun = float(shoot_metrics.get("gunfire_density") or 0)
    motion = float(shoot_metrics.get("center_motion") or 0)
    min_gun = float(os.environ.get("SHOOTER_AUTHOR_KILL_MIN_GUN", "0.060"))
    min_motion = float(os.environ.get("SHOOTER_AUTHOR_KILL_MIN_MOTION", "0.030"))

    if killfeed_density >= min_kf or ocr_kill:
        return True, "author_kill_feed", out
    if best_flash >= min_flash:
        return True, f"author_kill_hitflash={best_flash:.3f}", out
    # Strong POV spray with clear weapon edge ≈ author actively fragging.
    if best_weapon >= min_weapon and gun >= min_gun and motion >= min_motion:
        return True, f"author_kill_pov=weapon{best_weapon:.3f}", out
    return False, "no_author_kill", out


def author_kill_window_ok(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    profile: str = "standoff",
    shoot_metrics: dict | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """
    Montage/combat quality gate for author perspective.

    - Reject author-death / killcam dominated windows.
    - Require author-kill evidence (killfeed / hit flash / POV weapon+gun).
    """
    if not _env_flag("SHOOTER_AUTHOR_KILL_GATE", "1"):
        return True, "author_kill_gate_off", {}

    metrics: dict[str, Any] = {}
    death_on = _env_flag("SHOOTER_REJECT_AUTHOR_DEATH", "1")
    require_kill = _env_flag("SHOOTER_REQUIRE_AUTHOR_KILL", "1")

    has_kill, kill_reason, kill_m = detect_author_kill_signals(
        video_path,
        start_sec,
        duration_sec,
        profile=profile,
        shoot_metrics=shoot_metrics,
    )
    metrics.update({f"kill_{k}": v for k, v in kill_m.items()})
    metrics["has_author_kill"] = has_kill
    metrics["kill_reason"] = kill_reason

    if death_on:
        is_death, death_reason, death_m = detect_author_death_signals(
            video_path, start_sec, duration_sec
        )
        metrics.update({f"death_{k}": v for k, v in death_m.items() if k != "samples"})
        metrics["author_death"] = is_death
        if is_death and not has_kill:
            # Pure death / getting finished — never ship.
            return False, death_reason or "author_death", metrics
        if is_death and has_kill:
            # Author fragged then died: keep (kill is the content), note it.
            metrics["death_after_kill"] = True

    if require_kill and not has_kill:
        return False, kill_reason or "no_author_kill", metrics

    return True, kill_reason if has_kill else "author_kill_ok", metrics
