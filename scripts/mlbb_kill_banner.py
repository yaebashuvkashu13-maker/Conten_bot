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
    r"unstoppable|dominating|god|wiped|ace|slain|has\s+slain)"
    r"|you\s+have\s+been\s+slain|(?:has\s+been\s+)?slain\s+by|killed\s+by|defeated\s+by"
    r"|вражеск.{0,16}(?:тройн|трипл|маньяк|саваж|легенд|убий)"
    r"|противник.{0,16}(?:тройн|трипл|маньяк|саваж|убил)"
    r"|убит.{0,8}противник|убил.{0,8}вас|вас\s+убил"
    r")",
    re.I,
)

# Quick-chat / coordination overlays — not kill streak banners.
_COORDINATION_RE = re.compile(
    r"(?:"
    r"\b(?:gather|regroup|group\s*up|attack|retreat|defend|push|fall\s*back|"
    r"initiate|on\s+my\s+way|request\s+backup|clear\s+lane|start\s+a\s+revolt|"
    r"assemble|hold\s+on|wait\s+for\s+me|follow\s+me|split\s+push|"
    r"destroy\s+turret|take\s+turtle|lord|turtle)\b"
    r"|lord\s+spawned|turtle\s+spawned|lord\s+has\s+appeared|turtle\s+has\s+appeared|"
    r"el\s+lord\s+ha\s+aparecido|eso\s+fue\s+incre"
    r"|соберитесь|собраться|в\s+атаку|отступайте|отступить|защищайте|"
    r"на\s+меня|держите\s+линию|подкреплен|к\s+лорду|к\s+черепах|черепаха|"
    r"лорд\s+появил|черепаха\s+появил"
    r")",
    re.I,
)

# Objective announces that must always veto (even inside noisy OCR).
_STRONG_COORDINATION_RE = re.compile(
    r"(?:"
    r"lord\s+spawned|turtle\s+spawned|lord\s+has\s+appeared|turtle\s+has\s+appeared|"
    r"el\s+lord\s+ha\s+aparecido|take\s+turtle|take\s+lord|instant\s+kill|"
    r"gather(?:\s+at|\s+near|\s+up)?|"
    r"лорд\s+появил|черепаха\s+появил|к\s+лорду|к\s+черепах|соберитесь"
    r")",
    re.I,
)

# HUD OCR soup: clocks, ping ms, hero codes — stray "Retreat" here is not a real chat banner.
_HUD_OCR_SOUP_RE = re.compile(
    r"(?:\d+\s*ms\b|\b\d{1,2}[:.]\d{2}\b|\bT\d{3,}|\bX\d{1,}|\bBOnon\b|\b\d{1,2}\s*-\s*\d{1,2}\b)",
    re.I,
)

_KILL_STREAK_HINT_RE = re.compile(
    r"(?:"
    r"savage|maniac|triple|double|legendary|rampage|shutdown|first\s+blood|"
    # Y3In5vMdlak: RapidOCR reads UNSTOPPABLE; without it neg_ref:no_banner
    # vetoed real own-kill frames at presend.
    r"unstoppable|dominating|godlike|unstopable|"
    # RapidOCR often glues HAS SLAIN → HASSLAIN.
    r"has\s*slain|been\s*slain|killing\s+spree|убийств|саваж|маньяк|тройн|двойн"
    r")",
    re.I,
)

_STREAK_PATTERNS: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"savage|саваж", re.I), 5, "savage"),
    (re.compile(r"legendary|легендар", re.I), 5, "legendary"),
    (re.compile(r"maniac|маньяк", re.I), 4, "maniac"),
    (re.compile(r"ruthless|беспощад|безжалост", re.I), 4, "ruthless"),
    (re.compile(r"triple\s*kill|тройн.{0,12}убий", re.I), 3, "triple"),
    (re.compile(r"ultra\s*kill|mega\s*kill", re.I), 3, "triple"),
    (re.compile(r"double\s*kill|двойн.{0,12}убий|ou?ble\s*kill|d0uble|2\s*x\s*kill", re.I), 2, "double"),
    (re.compile(r"unstoppable|unstopable|dominating", re.I), 2, "double"),
    (re.compile(r"godlike", re.I), 3, "triple"),
    # Strong singles only — weak "kill" alone is handled as single_weak needing color.
    (
        re.compile(
            r"has\s*slain|been\s*slain|killing\s+spree|first\s+blood|"
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
    r"has\s*slain|been\s*slain|killing\s+spree|first\s+blood|shutdown|rampage|"
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


def _discover_active() -> bool:
    return os.environ.get("MLBB_BANNER_DISCOVER_ACTIVE", "0") == "1"


# Soft cap on Tesseract calls during one discover pass (hang guard).
_OCR_CALL_BUDGET: dict[str, int] = {"left": -1}


def reset_ocr_call_budget(n: int | None = None) -> None:
    """Reset per-discover OCR budget. n=None → env MLBB_DISCOVER_OCR_CALL_BUDGET (default 10)."""
    if n is None:
        n = max(0, int(os.environ.get("MLBB_DISCOVER_OCR_CALL_BUDGET", "10") or "10"))
    _OCR_CALL_BUDGET["left"] = int(n)


def _ocr_budget_ok() -> bool:
    left = int(_OCR_CALL_BUDGET.get("left", -1))
    return left != 0  # -1 = unlimited (non-discover / tests)


def _ocr_budget_consume() -> None:
    left = int(_OCR_CALL_BUDGET.get("left", -1))
    if left > 0:
        _OCR_CALL_BUDGET["left"] = left - 1


def ocr_weak_needs_hud(source: str, tier: int, own_reason: str) -> bool:
    """OCR/color tier≤2 without HUD portrait match must not ship (ally FP)."""
    src = str(source or "").lower()
    if src not in {"ocr", "color"} and src:
        # Empty source treated as OCR-like at call sites; named ref/hud ok.
        if not (src.startswith("ocr") or src.startswith("color")):
            return False
    if int(tier or 0) > 2:
        return False
    if os.environ.get("MLBB_OCR_SINGLE_REQUIRE_HUD", "1") != "1":
        return False
    return not str(own_reason or "").startswith("hud_killer_ok")


def _discover_merge_min_tier() -> int:
    """Discover hunts double+; singles were the HAS-SLAIN jog junk path."""
    return max(1, int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_MERGE_TIER", "2")))


def _collect_min_tier() -> int:
    """Collect/normalize accepts discover-tier banners; presend still floors at send_min_tier."""
    raw = os.environ.get("MLBB_KILL_BANNER_COLLECT_MIN_TIER")
    if raw is not None and str(raw).strip():
        return max(1, int(str(raw).strip()))
    return _discover_merge_min_tier()


def _ref_classify_min_tier() -> int:
    if _discover_active():
        return _discover_merge_min_tier()
    return _min_tier()


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

    Exception: own-kill ref singles already passed the HUD killer gate at
    discover — re-running deep OCR in presend hangs the feed for minutes.
    """
    trust_raw = os.environ.get(
        "MLBB_VOD_BANNER_PRESEND_TRUST_DISCOVER",
        os.environ.get("MLBB_VOD_PRESEND_TRUST_DISCOVERY", "0"),
    )
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
        or (row.get("clip") or {}).get("kill_banner_source")
        or ""
    )
    # Own-kill discover already gated HUD match; empty source is still the
    # own-kill ref path (OCR allies never reach the send pool).
    if (
        os.environ.get("MLBB_BANNER_OWN_KILL_REQUIRED", "0") == "1"
        and os.environ.get("MLBB_VOD_BANNER_PRESEND_TRUST_OWN_KILL", "1") == "1"
        and tier_i >= 1
        and (src.startswith("ref") or not src)
        and not src.startswith("ocr")
        and label not in {"single_weak", "color", "announce"}
    ):
        return True
    if str(trust_raw).strip() not in {"1", "true", "True", "yes"}:
        return False
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


def is_enemy_kill_text(text: str) -> bool:
    return bool(_ENEMY_STREAK_RE.search(str(text or "")))


def is_coordination_banner_text(text: str) -> bool:
    blob = str(text or "")
    if not blob:
        return False
    if _KILL_STREAK_HINT_RE.search(blob):
        return False
    # Lord/Turtle / clear Gather — always veto (AJ2 / 2Ww5).
    if _STRONG_COORDINATION_RE.search(blob):
        return True
    if not _COORDINATION_RE.search(blob):
        return False
    # Y3In5vMdlak: "05.03 T61118 X62 20 Retreat Vitality Cry" is HUD soup,
    # not a retreat quick-chat banner over a kill.
    if _HUD_OCR_SOUP_RE.search(blob) and len(blob) > 28:
        return False
    return True


# Presend neighbor OCR must not hang for minutes (RapidOCR on wrong frames).
_PRESEND_LIVE_OCR_LEFT: dict[str, int] = {"left": -1}


def _presend_live_ocr_budget_reset() -> None:
    _PRESEND_LIVE_OCR_LEFT["left"] = max(
        0, int(os.environ.get("MLBB_PRESEND_LIVE_OCR_BUDGET", "3") or "3")
    )


def _presend_live_ocr_budget_ok() -> bool:
    left = int(_PRESEND_LIVE_OCR_LEFT.get("left", -1))
    return left < 0 or left > 0


def _presend_live_ocr_budget_consume() -> None:
    left = int(_PRESEND_LIVE_OCR_LEFT.get("left", -1))
    if left > 0:
        _PRESEND_LIVE_OCR_LEFT["left"] = left - 1


def _live_overlay_text(
    frame, *, max_chars: int = 160, consume_presend_budget: bool = False
) -> str:
    """
    Live OCR for coordination/enemy vetoes and ref confirmation.

    Ref-bank hits carry canned text ("TRIPLE KILL") which hides live overlays
    like Take Turtle / Gather — AJ2o2jHhNfE_414 shipped a false triple that way.
    Prefer RapidOCR (Tesseract is blind on YT gold glyphs).
    """
    if frame is None:
        return ""
    if os.environ.get("MLBB_BANNER_LIVE_OVERLAY_OCR", "1") != "1":
        return ""
    if consume_presend_budget:
        if not _presend_live_ocr_budget_ok():
            return ""
        _presend_live_ocr_budget_consume()
    try:
        # Do not consume discover OCR budget — this is a safety veto, not a hunt.
        saved = int(_OCR_CALL_BUDGET.get("left", -1))
        _OCR_CALL_BUDGET["left"] = -1
        # Cap RapidOCR wall during presend neighbor search (default discover is 20s).
        saved_to = os.environ.get("MLBB_RAPID_OCR_TIMEOUT_SEC")
        if consume_presend_budget:
            os.environ["MLBB_RAPID_OCR_TIMEOUT_SEC"] = str(
                max(3, int(os.environ.get("MLBB_PRESEND_RAPID_OCR_TIMEOUT_SEC", "8") or "8"))
            )
        try:
            try:
                from mlbb_banner_ocr import read_banner_text

                text = read_banner_text(frame, prefer_rapid=True)
            except Exception:
                text = ""
            if not text:
                text = _ocr_banner_zones(frame, deep=False)
        finally:
            _OCR_CALL_BUDGET["left"] = saved
            if consume_presend_budget:
                if saved_to is None:
                    os.environ.pop("MLBB_RAPID_OCR_TIMEOUT_SEC", None)
                else:
                    os.environ["MLBB_RAPID_OCR_TIMEOUT_SEC"] = saved_to
        return " ".join(str(text or "").split())[:max_chars]
    except Exception:
        return ""


def classify_banner_text(text: str) -> KillBannerHit | None:
    blob = " ".join(str(text or "").split())
    if not blob:
        return None
    if is_enemy_kill_text(blob):
        return None
    if is_coordination_banner_text(blob):
        return None
    best_tier = 0
    best_label = ""
    for pat, tier, label in _STREAK_PATTERNS:
        if pat.search(blob) and tier > best_tier:
            best_tier = tier
            best_label = label
    if best_tier > 0:
        return KillBannerHit(sec=0.0, tier=best_tier, label=best_label, text=blob[:120])
    # OCR garbage (SAWAGE, DOUBLKILL, USTENE) — fuzzy map to known labels.
    try:
        from mlbb_banner_ocr import fuzzy_match_banner_label

        fuzzy = fuzzy_match_banner_label(blob)
    except Exception:
        fuzzy = None
    if fuzzy is None:
        return None
    _score, canon, tier, label = fuzzy
    return KillBannerHit(
        sec=0.0,
        tier=int(tier),
        label=str(label),
        text=f"{blob[:80]}~{canon}"[:120],
    )


def _announce_color_score(frame) -> float:
    """Gold/white pixel ratio in upper banner band (gate-compatible)."""
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


def _banner_structure_score(frame) -> float:
    """
    Structure prior for kill-announce flash (not a send gate).

    Real banners are a centered horizontal gold/white band with gold+white
    adjacency — farming HUD gold is more diffuse / edge-heavy. Used only to
    rank which decoded frames deserve ref/OCR, not to accept kills alone.
    """
    import cv2
    import numpy as np

    small = cv2.resize(frame, (320, 180))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    h, w = small.shape[:2]
    y0, y1 = int(h * 0.02), int(h * 0.30)
    x0, x1 = int(w * 0.15), int(w * 0.85)
    zone = hsv[y0:y1, x0:x1]
    if zone.size == 0:
        return 0.0
    gold = cv2.inRange(zone, np.array([8, 100, 140]), np.array([40, 255, 255]))
    white = cv2.inRange(zone, np.array([0, 0, 210]), np.array([180, 50, 255]))
    combined = cv2.bitwise_or(gold, white)
    area = float(combined.size)
    if area <= 0:
        return 0.0
    ratio = float(np.count_nonzero(combined)) / area
    # Horizontal band: mass concentrated in mid rows of the upper ROI.
    row_mass = np.count_nonzero(combined, axis=1).astype(np.float32)
    if float(row_mass.max()) <= 0:
        return min(1.0, ratio * 8.0)
    row_peak = float(row_mass.max() / max(combined.shape[1], 1))
    # Centrality: mass near horizontal center of the band.
    col_mass = np.count_nonzero(combined, axis=0).astype(np.float32)
    xs = np.arange(col_mass.size, dtype=np.float32)
    total = float(col_mass.sum()) + 1e-6
    cx = float((xs * col_mass).sum() / total)
    center = 0.5 * float(col_mass.size - 1)
    centrality = 1.0 - min(1.0, abs(cx - center) / max(center, 1.0))
    # Gold touching white (outline + fill) — typical announce look.
    dil = cv2.dilate(gold, np.ones((3, 3), np.uint8), iterations=1)
    adj = float(np.count_nonzero(cv2.bitwise_and(dil, white))) / area
    score = (
        min(1.0, ratio * 10.0) * 0.45
        + min(1.0, row_peak * 1.8) * 0.25
        + centrality * 0.15
        + min(1.0, adj * 18.0) * 0.15
    )
    return float(min(1.0, score))


def _banner_flash_score(frame) -> float:
    """Rank frames for classification: color × structure (discover ranking only)."""
    color = _announce_color_score(frame)
    if color < 0.004:
        return color
    # Full-zone gold wash (endcard / replay) scores ~0.5–1.0 and steals rank
    # from real thin announce bands (~0.01–0.08). Demote floods for ranking only.
    if color >= 0.45:
        color *= 0.22
    elif color >= 0.20:
        color *= 0.55
    try:
        struct = _banner_structure_score(frame)
    except Exception:
        struct = 0.0
    return float(min(1.0, color * 0.55 + struct * 0.45))


def _rank_fight_candidate_secs(
    frames: list[tuple[float, object]],
    *,
    focus_sec: float | None = None,
    max_classify: int = 6,
) -> list[float]:
    """
    Pick the most banner-like frames from a dense fight decode.

    Prefers temporal local maxima (flash in → flash out) so steady gold HUD
    loses to a real announce blink without lowering accept thresholds.
    """
    if not frames:
        return []
    scored: list[tuple[float, float]] = []
    for sec, frame in frames:
        scored.append((float(sec), _banner_flash_score(frame)))
    scored.sort(key=lambda row: row[0])
    # Temporal local-max boost vs neighbors (±1 sample).
    boosted: list[tuple[float, float]] = []
    for i, (sec, sc) in enumerate(scored):
        left = scored[i - 1][1] if i > 0 else 0.0
        right = scored[i + 1][1] if i + 1 < len(scored) else 0.0
        local = sc >= left - 1e-6 and sc >= right - 1e-6 and sc > 0.008
        boost = 1.25 if local else 1.0
        boosted.append((sec, sc * boost))
    boosted.sort(key=lambda row: row[1], reverse=True)
    floor = max(0.006, _color_min_score() * 0.12)
    picks: list[float] = []
    for sec, sc in boosted:
        if sc < floor and picks:
            break
        picks.append(sec)
        if len(picks) >= max_classify:
            break
    if focus_sec is not None and frames:
        nearest = min(frames, key=lambda row: abs(row[0] - focus_sec))[0]
        if nearest not in picks:
            picks.insert(0, nearest)
    return picks[: max_classify + 1]


def _ocr_banner_zones(frame, *, deep: bool = False) -> str:
    import cv2

    # Discover hang guard: stop after N zone-OCR calls per VOD pass.
    if _discover_active() and not _ocr_budget_ok():
        return ""
    if _discover_active():
        _ocr_budget_consume()

    # Prefer RapidOCR — Tesseract rarely reads YT gold banner glyphs.
    # Never during dense discover: each RapidOCR subprocess reloads the model
    # (~2s) and turns a 240s budget into a multi-hour crawl.
    use_rapid = os.environ.get("MLBB_BANNER_RAPID_OCR", "1") == "1"
    if _discover_active():
        phase = os.environ.get("MLBB_DISCOVER_PHASE", "peak")
        if os.environ.get("MLBB_BANNER_DISCOVER_RAPID", "0") == "1":
            pass  # Rapid on all discover probes (ops override)
        elif (
            os.environ.get("MLBB_BANNER_DISCOVER_RAPID_PEAKS", "1") == "1"
            and phase != "dense"
        ):
            pass  # Rapid on fight-first / peak probes only
        else:
            use_rapid = False
    if use_rapid:
        try:
            from mlbb_banner_ocr import read_banner_text

            rapid_text = read_banner_text(frame, prefer_rapid=True)
            if rapid_text and (
                classify_banner_text(rapid_text) is not None
                or sum(ch.isalpha() for ch in rapid_text) >= 6
            ):
                return rapid_text
        except Exception as exc:
            log.debug("rapid banner OCR failed: %s", exc)

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
    # Wide zone fan-out multiplies tesseract work — off by default.
    if os.environ.get("MLBB_KILL_BANNER_OCR_WIDE", "0") == "1":
        zones.extend(
            [
                small[int(h * 0.00) : int(h * 0.22), int(w * 0.05) : int(w * 0.95)],
                small[int(h * 0.10) : int(h * 0.40), int(w * 0.25) : int(w * 0.75)],
            ]
        )
    if deep:
        zones.append(small[int(h * 0.08) : int(h * 0.38), int(w * 0.02) : int(w * 0.38)])
    upscale = max(1.0, float(os.environ.get("MLBB_KILL_BANNER_OCR_UPSCALE", "2.0")))
    texts: list[str] = []
    # Deep used 4 PSMs × 2 variants × many zones → multi-minute hangs.
    psms = (7, 6) if deep else (7, 11)
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
        if deep and os.environ.get("MLBB_KILL_BANNER_OCR_INV", "0") == "1":
            variants.append(cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1])
        for variant in variants:
            for psm in psms:
                try:
                    tess_timeout = max(
                        2,
                        int(os.environ.get("MLBB_TESSERACT_TIMEOUT_SEC", "6") or "6"),
                    )
                    text = pytesseract.image_to_string(
                        variant,
                        config=f"--psm {psm} -l eng+rus",
                        timeout=tess_timeout,
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


def _classify_frame(
    sec: float,
    frame,
    *,
    deep: bool = False,
    allow_ocr: bool = True,
    vod: Path | None = None,
) -> KillBannerHit | None:
    """
    Classify a frame as a kill-streak banner.

    Prefer the owner screenshot bank (fast) before Tesseract OCR (slow / often blind).
    """
    if os.environ.get("MLBB_KILL_SCAN_SKIP_OCR", "0") == "1":
        # Live/presend stays OCR-blind (tesseract hangs sends). Discover may
        # still run a bounded OCR spike pass when explicitly allowed.
        discover_ocr = (
            allow_ocr
            and _discover_active()
            and os.environ.get("MLBB_KILL_DISCOVER_ALLOW_OCR", "1") == "1"
        )
        if not discover_ocr:
            allow_ocr = False
    color = _announce_color_score(frame)
    # Ref needs a real gold announce flash — half-threshold was matching farming HUD.
    ref_mul = float(os.environ.get("MLBB_BANNER_REF_COLOR_MUL", "1.25"))
    if _discover_active():
        ref_mul = min(
            ref_mul,
            float(os.environ.get("MLBB_BANNER_DISCOVER_REF_COLOR_MUL", "0.40")),
        )
    ref_color_gate = _color_min_score() * ref_mul
    # YT gold banners often score 0.01–0.03 (8pbq@579 color=0.012). A 0.031 gate
    # blocked ALL ref matches overnight → hits=0 for hours. Discover uses a low
    # absolute ceiling; own-kill finalize still rejects ally/farm.
    if _discover_active():
        ref_color_gate = min(
            ref_color_gate,
            float(os.environ.get("MLBB_BANNER_DISCOVER_COLOR_GATE_MAX", "0.010")),
        )
    if os.environ.get("MLBB_BANNER_REF_BEFORE_OCR", "1") == "1" and color >= ref_color_gate:
        try:
            from mlbb_banner_ref_match import classify_banner_reference

            ref_hit = classify_banner_reference(sec, frame, vod=vod)
            if ref_hit is not None:
                return _finalize_banner_hit(frame, ref_hit, vod=vod)
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
            return _finalize_banner_hit(frame, hit, vod=vod)
    if color >= _color_min_score():
        deep_text = _ocr_banner_zones(frame, deep=True)
        if is_enemy_kill_text(deep_text) or is_coordination_banner_text(deep_text):
            return None
        classified = classify_banner_text(deep_text)
        if classified is not None:
            hit = _accept_ocr_hit(classified)
            if hit is not None:
                return _finalize_banner_hit(frame, hit, vod=vod)
        # Color-only without readable streak text — try ref bank once more, else drop.
        try:
            from mlbb_banner_ref_match import classify_banner_reference

            ref_hit = classify_banner_reference(sec, frame, vod=vod)
            if ref_hit is not None:
                return _finalize_banner_hit(frame, ref_hit, vod=vod)
        except Exception:
            pass
        if os.environ.get("MLBB_KILL_BANNER_COLOR_ONLY", "0") == "1":
            hit = KillBannerHit(
                sec=round(sec, 2),
                tier=1,
                label="color",
                text=f"color={color:.3f}",
                source="color",
            )
            return _finalize_banner_hit(frame, hit, vod=vod)
    return None


# Per-discover counters for yield memory (ally-trap vs own-kill).
_OWN_KILL_STATS: dict[str, int] = {"accepts": 0, "rejects": 0}


def reset_own_kill_stats() -> None:
    _OWN_KILL_STATS["accepts"] = 0
    _OWN_KILL_STATS["rejects"] = 0


def own_kill_stats() -> dict[str, int]:
    return dict(_OWN_KILL_STATS)


def _finalize_banner_hit(frame, hit: KillBannerHit, *, vod: Path | None = None) -> KillBannerHit | None:
    if os.environ.get("MLBB_BANNER_OWN_KILL_REQUIRED", "1") != "1":
        _OWN_KILL_STATS["accepts"] = int(_OWN_KILL_STATS.get("accepts") or 0) + 1
        return hit
    # Draft / early-game (Y3In TeamPick @9s) must never enter the send pool.
    try:
        min_banner = float(os.environ.get("MLBB_PRESEND_MIN_BANNER_SEC", "90") or "90")
        if float(hit.sec) < min_banner:
            _OWN_KILL_STATS["rejects"] = int(_OWN_KILL_STATS.get("rejects") or 0) + 1
            log.info(
                "banner own_kill reject sec=%s tier=%s reason=banner_too_early=%.0f<%.0f",
                hit.sec,
                hit.tier,
                float(hit.sec),
                min_banner,
            )
            return None
    except Exception:
        pass
    try:
        from mlbb_banner_hero_match import validate_own_kill_frame

        # Merge ref/OCR hit text with LIVE frame OCR so turtle/gather vetoes apply
        # even when the ref bank label is a canned "TRIPLE KILL".
        live = _live_overlay_text(frame)
        ocr_text = " ".join(x for x in (str(hit.text or ""), live) if x).strip()
        if live and is_coordination_banner_text(live):
            _OWN_KILL_STATS["rejects"] = int(_OWN_KILL_STATS.get("rejects") or 0) + 1
            log.info(
                "banner own_kill reject sec=%s tier=%s reason=live_coordination:%s",
                hit.sec,
                hit.tier,
                live[:60],
            )
            return None
        if live and is_enemy_kill_text(live):
            _OWN_KILL_STATS["rejects"] = int(_OWN_KILL_STATS.get("rejects") or 0) + 1
            log.info(
                "banner own_kill reject sec=%s tier=%s reason=live_enemy:%s",
                hit.sec,
                hit.tier,
                live[:60],
            )
            return None
        # Ref-bank vision alone ships farm/turtle FX as TRIPLE. Live OCR can
        # upgrade/correct the canned label when it reads a streak phrase.
        # Do NOT reject on HUD chrome (player names): RapidOCR almost always
        # reads names in the banner zone, which starved all sends.
        src = str(getattr(hit, "source", "") or "").lower()
        if src.startswith("ref") and os.environ.get("MLBB_BANNER_REF_REQUIRE_OCR", "1") == "1":
            live_hit = classify_banner_text(live) if live else None
            # Explicit HAS SLAIN without multi words — never keep canned TRIPLE/DOUBLE
            # from chrome-similar double_triple refs (8pbq_581).
            live_l = str(live or "").lower()
            has_slain = (
                "has slain" in live_l
                or "hasslain" in live_l.replace(" ", "")
                or "been slain" in live_l
            )
            multi_word = any(
                w in live_l
                for w in (
                    "double kill",
                    "triple kill",
                    "maniac",
                    "savage",
                    "unstoppable",
                    "legendary",
                )
            )
            if has_slain and not multi_word:
                live_hit = KillBannerHit(
                    sec=hit.sec,
                    tier=1,
                    label="single",
                    text=str(live or "")[:120],
                    source="ref+ocr",
                )
            if live_hit is not None and int(live_hit.tier or 0) >= 1:
                if int(live_hit.tier) != int(hit.tier or 0) or str(live_hit.label) != str(hit.label):
                    hit = KillBannerHit(
                        sec=hit.sec,
                        tier=int(live_hit.tier),
                        label=str(live_hit.label),
                        text=str(live_hit.text or live)[:120],
                        source="ref+ocr",
                    )
                    ocr_text = " ".join(x for x in (str(hit.text or ""), live) if x).strip()
            elif os.environ.get("MLBB_BANNER_REF_REQUIRE_OCR_STRICT", "0") == "1":
                letters = sum(ch.isalpha() for ch in (live or ""))
                min_letters = int(os.environ.get("MLBB_BANNER_REF_OCR_MIN_LETTERS", "6"))
                if letters >= min_letters:
                    _OWN_KILL_STATS["rejects"] = int(_OWN_KILL_STATS.get("rejects") or 0) + 1
                    log.info(
                        "banner own_kill reject sec=%s tier=%s reason=ref_no_ocr_streak:%s",
                        hit.sec,
                        hit.tier,
                        (live or "")[:60],
                    )
                    return None
            # Ambiguous double_triple chrome without live multi proof → keep floor
            # at double only (already mapped), but never ship as proven multi via
            # OCR-blind strong-ref. Mark for send gates.
            if "double_triple" in str(hit.text or "").lower() and (
                live_hit is None or int(getattr(live_hit, "tier", 0) or 0) < 2
            ):
                hit = KillBannerHit(
                    sec=hit.sec,
                    tier=min(int(hit.tier or 2), 2),
                    label="double" if int(hit.tier or 0) >= 2 else str(hit.label),
                    text=str(hit.text or "")[:120],
                    source=str(getattr(hit, "source", "ref") or "ref"),
                )
        prev_phase = os.environ.get("MLBB_BANNER_OWN_KILL_PHASE")
        os.environ["MLBB_BANNER_OWN_KILL_PHASE"] = "discover"
        try:
            ok, reason = validate_own_kill_frame(frame, vod=vod, ocr_text=ocr_text)
        finally:
            if prev_phase is None:
                os.environ.pop("MLBB_BANNER_OWN_KILL_PHASE", None)
            else:
                os.environ["MLBB_BANNER_OWN_KILL_PHASE"] = prev_phase
        if not ok:
            _OWN_KILL_STATS["rejects"] = int(_OWN_KILL_STATS.get("rejects") or 0) + 1
            log.info("banner own_kill reject sec=%s tier=%s reason=%s", hit.sec, hit.tier, reason)
            return None
        # OCR singles/doubles: require HUD portrait match. Icon-only / unverifiable
        # accepts shipped ally doubles (AJxz second beat in #AJxzNqHrlyo_294).
        src = str(getattr(hit, "source", "") or "").lower()
        if ocr_weak_needs_hud(src, int(hit.tier or 0), str(reason)):
            _OWN_KILL_STATS["rejects"] = int(_OWN_KILL_STATS.get("rejects") or 0) + 1
            log.info(
                "banner own_kill reject sec=%s tier=%s reason=ocr_single_no_hud:%s",
                hit.sec,
                hit.tier,
                reason,
            )
            return None
        _OWN_KILL_STATS["accepts"] = int(_OWN_KILL_STATS.get("accepts") or 0) + 1
        log.debug("banner own_kill ok sec=%s reason=%s", hit.sec, reason)
    except Exception as exc:
        # Fail closed: shipping ally/enemy banners is worse than missing a hit.
        _OWN_KILL_STATS["rejects"] = int(_OWN_KILL_STATS.get("rejects") or 0) + 1
        log.warning("banner own_kill check failed sec=%s: %s", hit.sec, exc)
        return None
    return hit


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
    # Prefer structure-aware flash ranking when enabled (default on).
    if os.environ.get("MLBB_BANNER_FLASH_RANK", "1") == "1":
        return _rank_fight_candidate_secs(
            frames, focus_sec=focus_sec, max_classify=max_ocr
        )
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
        # Default ~8 frames; fight-dense path uses find_banner_near_peak instead.
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
        hit = _classify_frame(sec, frame, deep=deep, allow_ocr=allow_ocr, vod=vod)
        if hit is not None:
            hits.append(hit)
    if not hits and frames and not quick and allow_ocr:
        # Respect caller allow_ocr — never force OCR (deep crawl hung for minutes).
        for sec, frame in frames:
            if _discover_active() and not _ocr_budget_ok():
                break
            hit = _classify_frame(sec, frame, deep=True, allow_ocr=allow_ocr, vod=vod)
            if hit is not None and _banner_hit_source_ok(hit.source):
                hits.append(hit)
                break
    hits.sort(key=lambda h: (-h.tier, 0 if _banner_hit_source_ok(h.source) else 1, h.sec))
    return hits


def _fight_dense_scan_enabled() -> bool:
    return os.environ.get("MLBB_BANNER_FIGHT_DENSE_SCAN", "1") == "1"


def scan_fight_window_dense(
    vod: Path,
    peak_sec: float,
    *,
    allow_ocr: bool = True,
) -> list[KillBannerHit]:
    """
    One ffmpeg decode covering the whole post-fight banner window at high fps.

    Replaces separate base/+3/+6 quick probes that each decoded ~8 frames and
    still missed ~1s gold flashes. Ranking is structure+temporal; accept gates
    stay in `_classify_frame` / `_finalize_banner_hit` / send path.
    """
    before = float(os.environ.get("MLBB_KILL_BANNER_FIGHT_BEFORE", "1.5"))
    after = float(os.environ.get("MLBB_KILL_BANNER_FIGHT_AFTER", "12"))
    fps = max(2.0, float(os.environ.get("MLBB_KILL_BANNER_FIGHT_FPS", "4")))
    t0 = max(0.0, float(peak_sec) - before)
    t1 = float(peak_sec) + after
    span = max(0.5, t1 - t0)
    max_frames = max(12, int(os.environ.get("MLBB_KILL_BANNER_FIGHT_MAX_FRAMES", "48")))
    sample_count = max(8, min(max_frames, int(span * fps) + 1))
    frames = _ffmpeg_sample_frames(vod, t0, t1, sample_count)
    if not frames:
        # Fallback: slightly wider quick window.
        return scan_window(
            vod,
            t0,
            t1,
            focus_sec=peak_sec + 3.0,
            quick=True,
            allow_ocr=allow_ocr,
        )
    max_classify = max(
        3, int(os.environ.get("MLBB_KILL_BANNER_FIGHT_MAX_CLASSIFY", "6"))
    )
    focus = float(peak_sec) + 3.0
    picks = _rank_fight_candidate_secs(
        frames, focus_sec=focus, max_classify=max_classify
    )
    frame_map = {sec: frame for sec, frame in frames}
    hits: list[KillBannerHit] = []
    for sec in picks:
        frame = frame_map.get(sec)
        if frame is None:
            continue
        hit = _classify_frame(sec, frame, deep=False, allow_ocr=allow_ocr, vod=vod)
        if hit is not None:
            hits.append(hit)
            # First accepted own-kill banner is enough for this fight window.
            if _banner_hit_source_ok(hit.source):
                break
    hits.sort(key=lambda h: (-h.tier, 0 if _banner_hit_source_ok(h.source) else 1, h.sec))
    return hits


def find_banner_near_peak(
    vod: Path,
    peak_sec: float,
    *,
    quick: bool = False,
    allow_ocr: bool = True,
    min_tier: int | None = None,
) -> KillBannerHit | None:
    """Look for streak banner around motion peak (banner at/just after peak)."""
    if quick and _fight_dense_scan_enabled():
        hits = scan_fight_window_dense(vod, peak_sec, allow_ocr=allow_ocr)
    elif quick:
        fight_first = os.environ.get("MLBB_BANNER_FIGHT_FIRST", "1") == "1"
        # Fight-first: look mostly AFTER the fight spike (banner confirms the kill).
        default_before = "2" if fight_first else "10"
        default_after = "8" if fight_first else "10"
        before = float(os.environ.get("MLBB_KILL_BANNER_QUICK_BEFORE", default_before))
        after = float(os.environ.get("MLBB_KILL_BANNER_QUICK_AFTER", default_after))
        # Prefer focus slightly after peak so OCR lands on the banner flash.
        focus = peak_sec + (3.0 if fight_first else 0.0)
        hits = scan_window(
            vod,
            peak_sec - before,
            peak_sec + after,
            focus_sec=focus,
            quick=True,
            allow_ocr=allow_ocr,
        )
    else:
        before = float(os.environ.get("MLBB_KILL_BANNER_SCAN_BEFORE", "20"))
        after = float(os.environ.get("MLBB_KILL_BANNER_SCAN_AFTER", "10"))
        hits = scan_window(
            vod,
            peak_sec - before,
            peak_sec + after,
            focus_sec=peak_sec,
            allow_ocr=allow_ocr,
        )
    if not hits:
        return None
    need = min_tier if min_tier is not None else _min_tier()
    for hit in hits:
        if hit.tier >= need and _banner_hit_source_ok(hit.source):
            return hit
    if not _banner_required():
        for hit in hits:
            if hit.tier >= need:
                return hit
    return None


def banner_hit_from_clip_meta(clip_meta: dict | None) -> KillBannerHit | None:
    """Reuse discover/highlight banner fields without re-scanning the VOD."""
    if not clip_meta:
        return None
    tier = clip_meta.get("kill_banner_tier")
    if tier is None and clip_meta.get("kill_banner"):
        tier = (clip_meta.get("kill_banner") or {}).get("tier")
    try:
        tier_i = int(tier) if tier is not None else 0
    except (TypeError, ValueError):
        tier_i = 0
    if tier_i < _collect_min_tier():
        return None
    banner_sec = clip_meta.get("banner_sec")
    if banner_sec is None:
        banner_sec = clip_meta.get("peak_start")
    if banner_sec is None:
        return None
    label = str(clip_meta.get("kill_banner") or "")
    if isinstance(clip_meta.get("kill_banner"), dict):
        label = str((clip_meta.get("kill_banner") or {}).get("label") or label)
    src = str(
        clip_meta.get("banner_source")
        or clip_meta.get("kill_banner_source")
        or ""
    )
    if src and not _banner_hit_source_ok(src):
        return None
    return KillBannerHit(
        sec=float(banner_sec),
        tier=tier_i,
        label=label or "single",
        text=str(clip_meta.get("banner_text") or ""),
        source=src or "discover",
    )


def _adaptive_banner_scan_start(vod: Path, duration: float) -> float:
    """Earliest sec to scan for banners — short/medium VODs have fights before 5 min."""
    base = float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "300"))
    if duration <= 240:
        return 15.0
    if duration <= 480:
        return min(base, 90.0)
    if duration <= 1200:
        return min(base, 45.0)
    return base


def _dense_scan_enabled() -> bool:
    if os.environ.get("MLBB_VOD_BANNER_DENSE_SEC", "0") == "1":
        return True
    # Fight-first path: dense is a miss-streak fallback, not the default.
    if (
        os.environ.get("MLBB_BANNER_FIGHT_FIRST", "1") == "1"
        and os.environ.get("MLBB_VOD_DISCOVER_ALWAYS_DENSE", "0") == "1"
        and os.environ.get("MLBB_FIGHT_FIRST_ALLOW_ALWAYS_DENSE", "0") != "1"
    ):
        pass  # fall through to miss-streak only
    elif os.environ.get("MLBB_VOD_DISCOVER_ALWAYS_DENSE", "0") == "1":
        return True
    # After several empty discovery rounds, brute-force 1–2 Hz ref+OCR sweep.
    try:
        miss = int(os.environ.get("MLBB_VOD_DISCOVER_MISS_STREAK", "0") or "0")
        need = max(1, int(os.environ.get("MLBB_VOD_DISCOVER_DENSE_AFTER_MISS", "2")))
        if miss >= need:
            return True
    except ValueError:
        pass
    return False


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
    """
    Discover floor for merging hits.

    Discover is intentionally looser than send: collect single+ anchors so
  OCR/ref hits are not dropped before presend enforces double+.
    Title may promise savage/maniac (enables dense scan), but forcing discover
    to tier 5 made OCR-blind VODs return hits=0 for hours. Cap title influence
    unless MLBB_VOD_TITLE_FORCE_DISCOVER_TIER=1.
    """
    base = _discover_merge_min_tier()
    requested = min_tier if min_tier is not None else base
    title_need = _title_min_tier_override()
    want = max(base, requested, title_need) if title_need > 0 else max(base, requested)
    if os.environ.get("MLBB_VOD_TITLE_FORCE_DISCOVER_TIER", "0") == "1":
        return want
    cap = max(base, int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_TITLE_CAP", "1")))
    return min(want, cap)


def _discover_hit_target() -> int:
    """
    How many banners discover should try to collect before stopping early.

    MIN_HITS alone was too low with SEND_ALL_BANNERS — spike sweep stopped after
    2 hits and skipped the rest of the VOD. Montage mode also needs enough
    distinct banners to glue 3–4 fights.
    """
    want = max(1, int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_MIN_HITS", "1")))
    if os.environ.get("MLBB_VOD_MONTAGE", "0") == "1" and os.environ.get("MLBB_SKIP_MONTAGE", "0") != "1":
        mont = max(1, int(os.environ.get("MLBB_VOD_MONTAGE_MIN_CLIPS", "1")))
        want = max(want, mont)
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
    discover_saved = os.environ.get("MLBB_BANNER_DISCOVER_ACTIVE")
    os.environ["MLBB_BANNER_DISCOVER_ACTIVE"] = "1"
    reset_own_kill_stats()
    reset_ocr_call_budget()
    try:
        hits = _discover_vod_kill_banners_inner(
            vod,
            min_tier=min_tier,
            hint_peaks=hint_peaks,
        )
        try:
            from mlbb_vod_segment_store import vod_youtube_id
            from mlbb_vod_yield_memory import record_scan

            title = ""
            try:
                from mlbb_vod_title import vod_title_blob

                title = str(vod_title_blob(vod) or "")
            except Exception:
                title = str(vod.stem)
            stats = own_kill_stats()
            raw_banners = int(stats.get("accepts") or 0) + int(stats.get("rejects") or 0)
            if raw_banners <= 0:
                raw_banners = len(hits)
            record_scan(
                youtube_id=vod_youtube_id(vod),
                title=title,
                banner_hits=raw_banners,
                own_kill_hits=int(stats.get("accepts") or len(hits)),
                own_kill_rejects=int(stats.get("rejects") or 0),
            )
        except Exception as exc:
            log.debug("yield memory scan record skipped: %s", exc)
        return hits
    finally:
        if discover_saved is None:
            os.environ.pop("MLBB_BANNER_DISCOVER_ACTIVE", None)
        else:
            os.environ["MLBB_BANNER_DISCOVER_ACTIVE"] = discover_saved


def _discover_vod_kill_banners_inner(
    vod: Path,
    *,
    min_tier: int | None = None,
    hint_peaks: list[float] | None = None,
) -> list[KillBannerHit]:
    import numpy as np

    from mlbb_fight_segment import _analysis_for

    analysis = _analysis_for(vod)
    duration = float(analysis.get("duration") or 0.0)
    if duration < 20.0:
        return []
    need = _effective_discover_min_tier(min_tier)
    title_need = _title_min_tier_override()
    dense_tier = max(need, title_need, int(min_tier or 0))
    dense = _dense_scan_enabled()
    # Title-promised maniac/savage must not fall into the sparse peak+OCR path
    # that historically burned 240s on ~9 probes and returned 0 hits.
    if (
        not dense
        and dense_tier >= 4
        and os.environ.get("MLBB_VOD_TITLE_DENSE_AUTO", "1") == "1"
    ):
        dense = True
        log.info(
            "banner discover %s: auto-dense for title/min tier=%s (discover_floor=%s)",
            vod.name,
            dense_tier,
            need,
        )
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
    t_discover0 = time.monotonic()
    deadline = t_discover0 + max_sec
    # Peak+OCR historically exhausted the whole wall budget (~9 probes / 240s)
    # and never reached the spike/dense sweep. Always reserve time after peaks.
    # Fight-first: most budget goes to fight peaks + post-peak banner probes.
    fight_first = os.environ.get("MLBB_BANNER_FIGHT_FIRST", "1") == "1"
    if fight_first:
        default_peak_frac = "0.55"
    else:
        default_peak_frac = "0.20" if dense else "0.40"
    peak_frac = float(os.environ.get("MLBB_KILL_BANNER_DISCOVER_PEAK_BUDGET_FRAC", default_peak_frac))
    peak_frac = max(0.10, min(0.75 if fight_first else 0.60, peak_frac))
    peak_deadline = t_discover0 + max_sec * peak_frac
    hits: list[KillBannerHit] = []
    probes = 0
    want = _discover_hit_target()
    # Quota path: one fresh own-kill is enough to ship; extra OCR only burns the day.
    if os.environ.get("MLBB_DISCOVER_SHIP_ON_FIRST", "0") == "1":
        want = 1
    # Prefer stopping on first double+ (solo ship) while still hunting 2+ when
    # only singles are found (montage). Prevents 6h barren loops at want=2.
    ship_first_double = os.environ.get("MLBB_DISCOVER_SHIP_ON_FIRST_DOUBLE", "1") == "1"
    os.environ["MLBB_DISCOVER_PHASE"] = "peak"
    log.info(
        "banner discover %s: start dense=%s fight_first=%s max_probes=%s max_sec=%.0f "
        "peak_budget=%.0fs need_tier=%s want=%s",
        vod.name,
        dense,
        int(fight_first),
        max_probes,
        max_sec,
        max_sec * peak_frac,
        need,
        want,
    )

    exclude_secs: list[float] = []
    exclude_gap = float(os.environ.get("MLBB_BANNER_DISCOVER_EXCLUDE_GAP_SEC", "18") or "18")
    raw_exclude = str(os.environ.get("MLBB_BANNER_DISCOVER_EXCLUDE_SECS", "") or "").strip()
    if raw_exclude:
        for part in raw_exclude.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                exclude_secs.append(float(part))
            except ValueError:
                continue
        if exclude_secs:
            log.info(
                "banner discover %s: exclude %s prior sent peaks (gap=%.0fs)",
                vod.name,
                [int(x) for x in exclude_secs[:8]],
                exclude_gap,
            )

    def _near_excluded(sec: float) -> bool:
        for ex in exclude_secs:
            if abs(float(sec) - float(ex)) <= exclude_gap:
                return True
        return False

    def _merge_hit(hit: KillBannerHit) -> None:
        nonlocal want
        if hit.tier < need or not _banner_hit_source_ok(hit.source):
            return
        # Already-sent moments must not satisfy want=1 early-stop — keep scanning.
        if _near_excluded(hit.sec):
            log.info(
                "banner discover %s: skip hit t=%.1f tier=%s near already-sent",
                vod.name,
                hit.sec,
                hit.tier,
            )
            return
        # Absolute nearest-hit merge — peak order is intensity-ranked, not time order.
        # Signed `hit.sec - hits[-1].sec` previously collapsed distant earlier hits.
        dedupe = float(os.environ.get("MLBB_BANNER_DISCOVER_HIT_DEDUP_SEC", "6") or "6")
        best_i = -1
        best_gap = None
        for i, prev in enumerate(hits):
            gap = abs(float(hit.sec) - float(prev.sec))
            if gap < dedupe and (best_gap is None or gap < best_gap):
                best_i = i
                best_gap = gap
        if best_i >= 0:
            if hit.tier > hits[best_i].tier:
                hits[best_i] = hit
        else:
            hits.append(hit)
        # One own-kill double+ is enough to stop early and solo-ship (live OCR
        # still gates). Keep want>=2 only while all hits are singles (montage).
        if ship_first_double and any(int(h.tier or 0) >= 2 for h in hits):
            if want > 1:
                log.info(
                    "banner discover %s: double+ hit — ship-on-first-double want %s→1",
                    vod.name,
                    want,
                )
            want = 1

    def _budget_ok(*, peak_phase: bool = False) -> bool:
        limit = peak_deadline if peak_phase else deadline
        return probes < max_probes and time.monotonic() < limit

    def _probe_at(t: float, *, deep: bool, allow_ocr: bool = True, quick: bool = True) -> bool:
        nonlocal probes
        if not _budget_ok(peak_phase=False):
            return False
        probes += 1
        # Wider probe window catches banners slightly after the motion spike.
        half = float(os.environ.get("MLBB_KILL_BANNER_DISCOVER_PROBE_AFTER", "4.0"))
        before = float(os.environ.get("MLBB_KILL_BANNER_DISCOVER_PROBE_BEFORE", "2.0"))
        for hit in scan_window(
            vod,
            t - before,
            t + max(3.0, half),
            focus_sec=t,
            deep=deep,
            quick=quick,
            allow_ocr=allow_ocr,
        ):
            _merge_hit(hit)
            if hits:
                return True
        return _budget_ok(peak_phase=False)

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

    fight_first = os.environ.get("MLBB_BANNER_FIGHT_FIRST", "1") == "1"
    # Skip re-rank when highlight_scorer already fight-ranked hints (same analysis).
    already_ranked = os.environ.get("MLBB_BANNER_HINTS_FIGHT_RANKED", "0") == "1"
    if fight_first and peak_hints and not already_ranked:
        try:
            from mlbb_fight_segment import _analysis_for
            from mlbb_teamfight_detector import fight_first_peaks

            analysis = _analysis_for(vod)
            ranked = fight_first_peaks(analysis, peak_hints)
            if ranked:
                peak_hints = ranked
                log.info(
                    "banner discover %s: fight-first ranked peaks=%s",
                    vod.name,
                    len(peak_hints),
                )
        except Exception as exc:
            log.debug("fight-first re-rank skipped: %s", exc)
    # Prefer fights away from already-sent peaks so want=N finds fresh kills.
    if exclude_secs and peak_hints:
        fresh = [p for p in peak_hints if not _near_excluded(p)]
        stale = [p for p in peak_hints if _near_excluded(p)]
        if fresh:
            peak_hints = fresh + stale
            log.info(
                "banner discover %s: prefer %s fresh peaks (%s near already-sent)",
                vod.name,
                len(fresh),
                len(stale),
            )

    # Phase 1: peaks — ref-first (cheap), then OCR escalate, then a few full retries.
    # Keep this under peak_deadline so spike/dense still run.
    # Dense title path: ref-only on peaks (no OCR escalate) — dense sweep does the rest.
    # Fight-first: spend more budget on fight peaks; dense is fallback only.
    default_peak_hints = "12" if fight_first else "8"
    peak_limit = max(4, int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_PEAK_HINTS", default_peak_hints)))
    full_retry = 0 if dense else max(0, int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_PEAK_FULL_RETRY", "0")))
    missed_peaks: list[float] = []
    ocr_missed: list[float] = []
    # Kill banners flash AFTER the fight spike — fight-first probes post-peak harder.
    # Dense fight scan already covers peak-1.5..peak+12 at 4fps in ONE decode, so
    # separate +3/+6 offset probes only waste ffmpeg and OCR budget.
    post_offsets = [0.0]
    use_dense_fight = _fight_dense_scan_enabled()
    if (
        not use_dense_fight
        and os.environ.get("MLBB_KILL_BANNER_DISCOVER_POST_PEAK", "1") == "1"
    ):
        default_offs = "3,5,8" if fight_first else "2,4"
        raw_offs = os.environ.get("MLBB_KILL_BANNER_DISCOVER_POST_PEAK_OFFSETS", default_offs)
        for part in raw_offs.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                off = float(part)
            except ValueError:
                continue
            if off > 0 and off not in post_offsets:
                post_offsets.append(off)
    post_peak_max = max(
        0,
        int(
            os.environ.get(
                "MLBB_KILL_BANNER_DISCOVER_POST_PEAK_MAX",
                str(peak_limit if fight_first else 4),
            )
        ),
    )
    if use_dense_fight:
        log.info(
            "banner discover %s: fight-dense scan on (fps=%s window=-%s/+%s max_frames=%s)",
            vod.name,
            os.environ.get("MLBB_KILL_BANNER_FIGHT_FPS", "4"),
            os.environ.get("MLBB_KILL_BANNER_FIGHT_BEFORE", "1.5"),
            os.environ.get("MLBB_KILL_BANNER_FIGHT_AFTER", "12"),
            os.environ.get("MLBB_KILL_BANNER_FIGHT_MAX_FRAMES", "48"),
        )
    for peak_i, base_peak in enumerate(list(dict.fromkeys(peak_hints))[:peak_limit]):
        base_hit = False
        offsets = post_offsets if peak_i < post_peak_max else [0.0]
        for off in offsets:
            if not _budget_ok(peak_phase=True):
                break
            if len(hits) >= want:
                break
            peak = max(0.0, float(base_peak) + off)
            probes += 1
            hit = find_banner_near_peak(vod, peak, quick=True, allow_ocr=True)
            if hit:
                _merge_hit(hit)
                base_hit = True
                break
        if not base_hit:
            missed_peaks.append(float(base_peak))
        if not _budget_ok(peak_phase=True) or len(hits) >= want:
            break
    if not dense:
        for peak in missed_peaks:
            if not _budget_ok(peak_phase=True):
                break
            if len(hits) >= want:
                break
            probes += 1
            hit = find_banner_near_peak(vod, peak, quick=True, allow_ocr=True)
            if hit:
                _merge_hit(hit)
            else:
                ocr_missed.append(peak)
        for peak in ocr_missed[:full_retry]:
            if not _budget_ok(peak_phase=True):
                break
            if len(hits) >= want:
                break
            # Need ~15s+ left overall so spike still gets a real window.
            if (deadline - time.monotonic()) < 20.0:
                break
            if not _ocr_budget_ok():
                break
            probes += 1
            # Quick+OCR only — deep scan_window was a multi-minute hang vector.
            hit = find_banner_near_peak(vod, peak, quick=True, allow_ocr=True)
            if hit:
                _merge_hit(hit)
    if not dense:
        log.info(
            "banner discover %s: peak-phase probes=%s hits=%s/%s elapsed=%.0fs "
            "(budget=%.0fs/%.0fs) need_tier=%s",
            vod.name,
            probes,
            len(hits),
            want,
            time.monotonic() - t_discover0,
            max_sec * peak_frac,
            max_sec,
            need,
        )
    else:
        log.info(
            "banner discover %s: peak-phase-before-dense probes=%s hits=%s/%s "
            "elapsed=%.0fs need_tier=%s",
            vod.name,
            probes,
            len(hits),
            want,
            time.monotonic() - t_discover0,
            need,
        )

    # Throughput: if fight peaks found zero banners, do not burn the full remaining
    # wall on sparse ref/OCR — but NEVER return empty without a short spike.
    # Abort-to-empty starved Aug1 (Cv7 "11-Kill" + _lz95): 2 slow probes → 0 hits → hard skip.
    if (
        fight_first
        and not hits
        and (
            probes >= max(2, int(os.environ.get("MLBB_FIGHT_FIRST_MISS_MIN_PROBES", "3")))
            or time.monotonic() >= peak_deadline
        )
        and os.environ.get("MLBB_FIGHT_FIRST_ABORT_ON_MISS", "0") == "1"
    ):
        kill_rich = False
        try:
            from mlbb_vod_title import title_kill_count, title_promises_kill_streak, vod_title_blob

            blob = str(vod_title_blob(vod) or vod.stem).lower()
            # 8+ kills (incl. "11-Kill") or streak words → slightly longer spike.
            kill_rich = title_promises_kill_streak(blob) or title_kill_count(blob) >= 8
        except Exception:
            kill_rich = False
        if os.environ.get("MLBB_FIGHT_FIRST_KILL_RICH_SPIKE", "1") == "1":
            # Cap remaining wall so spike cannot recreate the 8-minute dead burn.
            # Do NOT flip dense=True here: dense runs before spike and returns
            # early (fX76 scanned t=3..19 only, skipped OCR spike entirely).
            default_sec = "90" if kill_rich else "45"
            default_probes = "12" if kill_rich else "8"
            remain = max(
                20.0,
                float(os.environ.get("MLBB_FIGHT_FIRST_KILL_RICH_SPIKE_SEC", default_sec)),
            )
            # Raise overall ceiling so spike actually gets the remain window.
            deadline = max(deadline, time.monotonic() + remain)
            max_probes = max(
                max_probes,
                probes
                + max(4, int(os.environ.get("MLBB_FIGHT_FIRST_KILL_RICH_SPIKE_PROBES", default_probes))),
            )
            # Ref-only spikes often miss YT gold glyphs; allow OCR spikes on miss.
            ocr_n = "6" if kill_rich else "3"
            cur_ocr = int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_OCR_SPIKES", "0") or "0")
            if cur_ocr < int(ocr_n):
                os.environ["MLBB_KILL_BANNER_DISCOVER_OCR_SPIKES"] = os.environ.get(
                    "MLBB_FIGHT_FIRST_MISS_OCR_SPIKES", ocr_n
                )
            # Peak-phase OCR often burns the whole discover budget (default 10),
            # then miss OCR spikes log ocr_spikes=N but execute 0 (UEr0/MUk6/9wsA).
            miss_ocr = max(
                1,
                int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_OCR_SPIKES", ocr_n) or ocr_n),
            )
            left = int(_OCR_CALL_BUDGET.get("left", -1))
            if 0 <= left < miss_ocr:
                _OCR_CALL_BUDGET["left"] = miss_ocr
                log.info(
                    "banner discover %s: ocr budget refresh left=%s→%s for miss spike",
                    vod.name,
                    left,
                    miss_ocr,
                )
            log.info(
                "banner discover %s: fight-first miss — short spike "
                "kill_rich=%s remain=%.0fs max_probes=%s ocr_spikes=%s",
                vod.name,
                int(kill_rich),
                remain,
                max_probes,
                os.environ.get("MLBB_KILL_BANNER_DISCOVER_OCR_SPIKES", "0"),
            )
        else:
            hits.sort(key=lambda h: h.sec)
            log.info(
                "banner discover %s: fight-first miss abort probes=%s elapsed=%.0fs — skip spike",
                vod.name,
                probes,
                time.monotonic() - t_discover0,
            )
            return hits

    # Dense 1 Hz sweep for title-promised savage/maniac VODs (or explicit ops flag).
    # Fight-first: skip dense when fight peaks already found enough own-kill banners.
    if (
        fight_first
        and len(hits) >= want
        and os.environ.get("MLBB_FIGHT_FIRST_DENSE_WHEN_HIT", "0") != "1"
    ):
        log.info(
            "banner discover %s: fight-first done hits=%s/%s — skip dense",
            vod.name,
            len(hits),
            want,
        )
    elif dense and probes < max_probes and time.monotonic() < deadline:
        os.environ["MLBB_DISCOVER_PHASE"] = "dense"
        t0 = _discover_scan_start(vod, duration)
        if peak_hints:
            hint_floor = max(
                0.0,
                min(float(p) for p in peak_hints)
                - float(os.environ.get("MLBB_KILL_BANNER_DISCOVER_HINT_PAD", "10")),
            )
            if not hits:
                # Peak-phase already missed — jump dense to fight region, not draft @3s.
                if hint_floor > t0:
                    log.info(
                        "banner discover %s: dense jump to fights %.0fs (was %.0fs)",
                        vod.name,
                        hint_floor,
                        t0,
                    )
                    t0 = hint_floor
            elif hint_floor < t0:
                log.info(
                    "banner discover %s: dense hint floor %.0fs (was %.0fs)",
                    vod.name,
                    hint_floor,
                    t0,
                )
                t0 = hint_floor
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
            os.environ.get("MLBB_KILL_BANNER_DENSE_COLOR_MUL", "0.45")
        )
        # Dense sweep: periodic OCR even on generic VODs — white/EN banners sit under color floor.
        title_ocr_every = max(
            0,
            int(os.environ.get("MLBB_KILL_BANNER_TITLE_OCR_EVERY", "3")),
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
        force_full_dense = os.environ.get("MLBB_VOD_BANNER_DISCOVER_FULL", "0") == "1"
        try:
            while t < duration - 2.0 and probes < max_probes and time.monotonic() < deadline:
                if len(hits) >= want and not force_full_dense:
                    log.info(
                        "banner discover %s: dense early-stop hits=%s want=%s t=%.0fs probes=%s",
                        vod.name,
                        len(hits),
                        want,
                        t,
                        probes,
                    )
                    break
                probes += 1
                frame = _read_frame_at(vod, t, dense_cap)
                if frame is not None:
                    color = _announce_color_score(frame)
                    force_ocr = title_ocr_every > 0 and (step_i % title_ocr_every == 0)
                    if color >= color_floor or force_ocr:
                        # Ref/color-cheap first; OCR on stronger flashes or title cadence.
                        hit = _classify_frame(t, frame, deep=False, allow_ocr=False, vod=vod)
                        if hit is None and (
                            force_ocr or color >= color_floor * 1.15
                        ):
                            hit = _classify_frame(t, frame, deep=False, allow_ocr=True, vod=vod)
                        if hit is not None:
                            before_n = len(hits)
                            _merge_hit(hit)
                            if len(hits) > before_n:
                                # Jump past this fight — dense 1.5s crawl after a hit
                                # burned minutes scanning the same teamfight.
                                gap = max(
                                    dense_step,
                                    float(os.environ.get("MLBB_KILL_BANNER_DENSE_HIT_GAP", "22")),
                                )
                                t += gap
                                step_i += 1
                                continue
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
        # Empty dense must not skip spike — that starved Aug1 kill-rich VODs.
        if hits:
            return hits
        log.info(
            "banner discover %s: dense miss — continue to spike hits=0 probes=%s",
            vod.name,
            probes,
        )

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

        def _run_spike_pass(*, allow_ocr: bool, label: str, stop_at: int, quick: bool = True) -> None:
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
                if not _probe_at(t, deep=not quick, allow_ocr=allow_ocr, quick=quick):
                    break
                if hits:
                    known.add(round(hits[-1].sec / 4.0))

        # Reserve OCR slots — ref pass used to burn max_probes=20 and leave ocr=0
        # (Cv7/nFf2 Aug1: ocr_spikes=6 logged but never executed).
        ocr_spikes = max(
            0,
            int(os.environ.get("MLBB_KILL_BANNER_DISCOVER_OCR_SPIKES", "0")),
        )
        ref_probe_ceiling = max_probes
        if ocr_spikes > 0 and len(hits) < want:
            ref_probe_ceiling = max(probes + 4, max_probes - ocr_spikes)
        saved_max = max_probes
        max_probes = min(max_probes, ref_probe_ceiling)
        _run_spike_pass(allow_ocr=False, label="ref", stop_at=want, quick=True)
        max_probes = max(saved_max, probes + ocr_spikes)
        if len(hits) < want and probes < max_probes and time.monotonic() < deadline:
            if ocr_spikes > 0 and _ocr_budget_ok():
                idxs = idxs[:ocr_spikes]
                log.info(
                    "banner discover %s: ocr spike pass n=%s probes=%s/%s",
                    vod.name,
                    ocr_spikes,
                    probes,
                    max_probes,
                )
                _run_spike_pass(allow_ocr=True, label="ocr", stop_at=want, quick=True)

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

    End is hard-capped at last_kill_banner + tier-aware post (singles ~2s,
    doubles ~4s, streaks ~5.5s) so mid-combo kills are not chopped while
    post-fight lane jogging is still cut.
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
    tier = int(banner_tier or 0)
    post = banner_post_sec(tier)
    banner = float(banner_sec)
    file_dur = float(file_dur)

    # Hard rule: stop shortly after the kill banner (last kill of this moment).
    end = min(file_dur, banner + post)
    # Lead window only — fight_start may TRIM idle preroll on singles/doubles,
    # never extend earlier (old min(fight_start, banner-lead) pulled 18s heads).
    # Tier≥3 (triple+): keep full streak lead — fight detector often starts mid-combo.
    start = max(0.0, banner - lead)
    if fight_start is not None and tier <= 2:
        fs = float(fight_start)
        if start < fs < banner - 1.0:
            start = fs

    if banner < start:
        start = max(0.0, banner - lead)
    if banner > end:
        end = min(file_dur, banner + max(post, 2.0))

    dur = end - start
    need = max(
        min_d,
        ideal_clip_min_sec(tier) if os.environ.get("MLBB_BANNER_IDEAL_MIN", "1") == "1" else min_d,
    )
    earliest = max(0.0, banner - lead)

    # Prefer longer pre-roll over longer post — never grow past banner+post
    # and never start earlier than banner-lead.
    if dur < need:
        start = max(earliest, end - need)
        if banner < start:
            start = earliest
        dur = end - start
    if dur < min_d:
        # Only stretch end when we cannot get min_d from pre-roll (early-file banner).
        deficit = min_d - dur
        if start <= 0.05 and deficit > 0.05:
            end = min(file_dur, end + deficit)
            dur = end - start
        else:
            start = max(earliest, end - min_d)
            if banner < start:
                start = earliest
                end = min(file_dur, max(end, banner + post))
            dur = end - start

    if dur > hard_max:
        start = max(earliest, end - hard_max)
        if banner < start:
            start = earliest
            end = min(file_dur, start + hard_max)
        dur = end - start
    elif dur > max_d:
        start = max(earliest, end - max_d)
        if banner < start:
            start = earliest
            end = min(file_dur, start + max_d)
        dur = end - start

    # With hard post-cut the kill sits near the end on purpose — do NOT pull
    # start earlier to "center" the banner (that recreated long idle heads).
    if os.environ.get("MLBB_BANNER_HARD_POST_CUT", "1") != "1":
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
    *,
    clip_meta: dict | None = None,
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

    collect_need = _collect_min_tier()
    hit = banner_hit_from_clip_meta(clip_meta)
    if hit is None:
        hit = find_banner_near_peak(vod, peak_sec, quick=True, min_tier=collect_need)
    if hit is None:
        hit = find_banner_near_peak(vod, peak_sec, quick=False, min_tier=collect_need)

    if _motion_anchor_ok():
        if hit is not None and hit.tier >= collect_need:
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

    if hit is None or hit.tier < collect_need:
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
