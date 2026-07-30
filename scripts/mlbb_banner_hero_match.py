#!/usr/bin/env python3
"""Own-hero kill banner validation via killer portrait vs hero icon bank."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import logging

log = logging.getLogger("mlbb_banner_hero_match")


def hero_icon_root() -> Path:
    env = os.environ.get("MLBB_HERO_ICON_ROOT", "").strip()
    if env:
        return Path(env)
    repo = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if repo:
        cand = Path(repo) / "data" / "mlbb_hero_icons"
        if cand.exists():
            return cand
    return Path(__file__).resolve().parent.parent / "data" / "mlbb_hero_icons"


def hero_match_enabled() -> bool:
    return os.environ.get("MLBB_BANNER_HERO_MATCH", "1") == "1"


def icon_library_ready() -> bool:
    root = hero_icon_root()
    if not root.exists():
        return False
    return any(root.rglob("*.png"))


def extract_killer_portrait_patch(frame) -> object | None:
    """
    Killer portrait sits on the right side of the kill-notification strip.
    Victim portrait is left — matching victim causes enemy-kill false positives.
    """
    try:
        import cv2
        from mlbb_banner_ref_match import extract_banner_zone_patch
    except Exception:
        return None
    banner = extract_banner_zone_patch(frame)
    if banner is None:
        return None
    _h, w = banner.shape[:2]
    if w < 40:
        return None
    x0, x1 = int(w * 0.58), int(w * 0.98)
    patch = banner[:, x0:x1]
    if patch.size == 0:
        return None
    return cv2.resize(patch, (48, 48))


def _icon_paths_for_hero(hero_id: str) -> list[Path]:
    hid = str(hero_id or "").strip().lower()
    if not hid:
        return []
    root = hero_icon_root() / hid
    if not root.exists():
        return []
    names = ("icon.png", "default.png", "portrait.png")
    out: list[Path] = []
    for name in names:
        p = root / name
        if p.exists():
            out.append(p)
    out.extend(sorted(root.glob("*.png")))
    return list(dict.fromkeys(out))


@lru_cache(maxsize=256)
def _load_icon_gray(path: str):
    import cv2

    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    return cv2.resize(img, (48, 48))


def portrait_match_score(patch, icon_path: Path) -> float:
    try:
        import cv2
        import numpy as np
        from mlbb_banner_ref_match import patch_hist_similarity, patch_edge_similarity
    except Exception:
        return 0.0
    icon = _load_icon_gray(str(icon_path))
    if icon is None or patch is None:
        return 0.0
    if len(getattr(patch, "shape", ())) == 3:
        patch = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    patch = cv2.resize(patch, (48, 48))
    # Blend structural + color-robust scores.
    hist = patch_hist_similarity(
        cv2.cvtColor(patch, cv2.COLOR_GRAY2BGR),
        cv2.cvtColor(icon, cv2.COLOR_GRAY2BGR),
    )
    edge = patch_edge_similarity(
        cv2.cvtColor(patch, cv2.COLOR_GRAY2BGR),
        cv2.cvtColor(icon, cv2.COLOR_GRAY2BGR),
    )
    return float(0.55 * edge + 0.45 * hist)


def best_hero_match(frame, *, hero_ids: tuple[str, ...] | None = None) -> tuple[str, float] | None:
    if not hero_match_enabled():
        return None
    patch = extract_killer_portrait_patch(frame)
    if patch is None:
        return None
    if hero_ids is None:
        try:
            from mlbb_hero_roles import load_heroes

            hero_ids = tuple(str(h.get("id") or "") for h in load_heroes() if h.get("id"))
        except Exception:
            hero_ids = tuple()
    best: tuple[str, float] | None = None
    min_sim = float(os.environ.get("MLBB_BANNER_HERO_ICON_MIN_SIM", "0.42"))
    for hid in hero_ids:
        for icon_path in _icon_paths_for_hero(hid):
            score = portrait_match_score(patch, icon_path)
            if score < min_sim:
                continue
            if best is None or score > best[1]:
                best = (hid, score)
    return best


def validate_own_kill_frame(
    frame,
    *,
    vod: Path | None = None,
    title: str = "",
    ocr_text: str = "",
) -> tuple[bool, str]:
    """
    Return (ok, reason).

    When icon bank is populated, killer portrait must match the played hero.
    Without icons, fall back to OCR enemy/coordination gates + negative ref bank.
    """
    if os.environ.get("MLBB_BANNER_OWN_KILL_REQUIRED", "1") != "1":
        return True, "own_kill_check_off"

    try:
        from mlbb_kill_banner import is_coordination_banner_text, is_enemy_kill_text
    except Exception:
        is_coordination_banner_text = lambda _t: False  # noqa: E731
        is_enemy_kill_text = lambda _t: False  # noqa: E731

    if ocr_text and is_coordination_banner_text(ocr_text):
        return False, "coordination_text"
    if ocr_text and is_enemy_kill_text(ocr_text):
        return False, "enemy_kill_text"

    try:
        from mlbb_banner_ref_match import match_negative_banner_reference

        neg = match_negative_banner_reference(frame)
        if neg is not None:
            score, reason, _path = neg
            reason_l = str(reason or "").lower()
            if any(
                k in reason_l
                for k in (
                    "enemy",
                    "coordination",
                    "quick_chat",
                    "no_banner",
                    "gather",
                    "retreat",
                )
            ):
                return False, f"neg_ref:{reason}"
            if float(score) >= float(os.environ.get("MLBB_BANNER_NEG_REF_MIN_SIM", "0.42")):
                return False, f"neg_ref:{reason}"
    except Exception:
        pass

    played: str | None = None
    if vod is not None:
        try:
            from mlbb_hero_roles import played_hero_from_vod

            played = played_hero_from_vod(vod, title=title)
        except Exception:
            played = None
    if not played and title:
        try:
            from mlbb_hero_roles import hero_from_text

            played = hero_from_text(title)
        except Exception:
            played = None

    if icon_library_ready() and played:
        match = best_hero_match(frame, hero_ids=(played,))
        if match is None:
            # Wrong hero on killer portrait — likely enemy kill banner.
            any_match = best_hero_match(frame)
            if any_match and any_match[0] != played:
                return False, f"killer_not_played:{any_match[0]}!={played}"
            return False, "killer_icon_miss"
        return True, f"killer_icon_ok:{played}:{match[1]:.3f}"

    if icon_library_ready() and not played:
        # Unknown played hero — reject portraits that strongly match a different hero
        # only when OCR also lacks a streak phrase (handled upstream).
        return True, "icon_skip_no_played_hero"

    return True, "icon_bank_empty"
