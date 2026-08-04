#!/usr/bin/env python3
"""Own-hero kill banner validation via killer portrait vs HUD / hero icon bank."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("mlbb_banner_hero_match")

# Full-frame fractions: MLBB kill banner puts killer LEFT of streak text,
# victim RIGHT. Status-bar portrait sits on the left edge of the bottom bar
# (often near mid-left on YT captures, classic bottom-left on mobile).
_KILLER_BOX = (0.07, 0.20, 0.39, 0.45)  # y0,y1,x0,x1
_HUD_BOXES = (
    (0.85, 0.97, 0.29, 0.35),  # centered bottom status bar (common YT)
    (0.86, 0.98, 0.01, 0.08),  # classic mobile bottom-left
    (0.86, 0.98, 0.22, 0.30),  # bar shifted left
)


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


def _crop_frac(frame, box: tuple[float, float, float, float]):
    import cv2

    if frame is None:
        return None
    h, w = frame.shape[:2]
    if h < 80 or w < 160:
        return None
    y0, y1, x0, x1 = box
    patch = frame[int(h * y0) : int(h * y1), int(w * x0) : int(w * x1)]
    if patch is None or patch.size == 0:
        return None
    return cv2.resize(patch, (48, 48))


def _patch_std(patch) -> float:
    try:
        import numpy as np

        if patch is None or getattr(patch, "size", 0) == 0:
            return 0.0
        return float(np.std(patch))
    except Exception:
        return 0.0


def _patch_usable(patch) -> bool:
    min_std = float(os.environ.get("MLBB_BANNER_HUD_MIN_STD", "18"))
    return _patch_std(patch) >= min_std


def extract_killer_portrait_patch(frame) -> object | None:
    """
    Killer portrait sits on the LEFT of the kill-notification strip
    (victim is on the right). Matching the right/victim side causes ally/enemy
    kill false positives.
    """
    try:
        import cv2  # noqa: F401
    except Exception:
        return None
    env_box = (os.environ.get("MLBB_BANNER_KILLER_BOX") or "").strip()
    box = _KILLER_BOX
    if env_box:
        try:
            parts = tuple(float(x) for x in env_box.split(","))
            if len(parts) == 4:
                box = parts  # type: ignore[assignment]
        except Exception:
            pass
    return _crop_frac(frame, box)


def extract_hud_hero_portrait_patch(frame) -> object | None:
    """Played-hero portrait from the bottom status bar (best usable crop)."""
    try:
        import cv2  # noqa: F401
    except Exception:
        return None
    scored: list[tuple[int, float, object]] = []
    for idx, box in enumerate(_HUD_BOXES):
        patch = _crop_frac(frame, box)
        if patch is None:
            continue
        s = _patch_std(patch)
        if s < float(os.environ.get("MLBB_BANNER_HUD_MIN_STD", "18")):
            continue
        scored.append((idx, s, patch))
    if not scored:
        return None
    max_std = max(s for _, s, _ in scored)
    # Prefer earlier layout priors when std is close (center bar > classic left).
    near = [(i, s, p) for i, s, p in scored if s >= max_std * 0.97]
    near.sort(key=lambda t: t[0])
    return near[0][2]


def patch_pair_score(patch_a, patch_b) -> float:
    try:
        import cv2
        from mlbb_banner_ref_match import patch_edge_similarity, patch_hist_similarity
    except Exception:
        return 0.0
    if patch_a is None or patch_b is None:
        return 0.0
    a = cv2.resize(patch_a, (48, 48))
    b = cv2.resize(patch_b, (48, 48))
    hist = patch_hist_similarity(a, b)
    edge = patch_edge_similarity(a, b)
    return float(0.55 * edge + 0.45 * hist)


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
        from mlbb_banner_ref_match import patch_edge_similarity, patch_hist_similarity
    except Exception:
        return 0.0
    icon = _load_icon_gray(str(icon_path))
    if icon is None or patch is None:
        return 0.0
    if len(getattr(patch, "shape", ())) == 3:
        patch = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    patch = cv2.resize(patch, (48, 48))
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

    Primary gate: killer (banner LEFT) must match the played-hero HUD portrait.
    Fallback: killer portrait vs title/VOD hero icon bank.
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
            _score, reason, _path = neg
            reason_l = str(reason or "").lower()
            # Enemy/coordination always veto.
            if any(
                k in reason_l
                for k in (
                    "enemy",
                    "coordination",
                    "quick_chat",
                    "gather",
                    "retreat",
                    "attack",
                )
            ):
                return False, f"neg_ref:{reason}"
            # not_kill/no_banner: veto when live OCR has no streak phrase.
            # Floor 0.35 (was 0.48): OZLs Aug1 HUD-matched empty jungle while
            # neg_ref:no_banner scored ~0.40 and still shipped.
            # Exception: strong labeled owner pos (double/triple/…) already proved
            # the banner visually — YT gold OCR is blind and no_banner ties pos
            # (8pbq@579 pos=0.611 vs no_banner=0.614) which starved overnight.
            if (
                ("not_kill" in reason_l or "no_banner" in reason_l)
                and float(_score)
                >= float(os.environ.get("MLBB_BANNER_NEG_NOT_KILL_MIN", "0.35"))
            ):
                has_streak = False
                if ocr_text:
                    try:
                        from mlbb_kill_banner import (
                            _KILL_STREAK_HINT_RE,
                            classify_banner_text,
                        )

                        has_streak = bool(_KILL_STREAK_HINT_RE.search(str(ocr_text)))
                        if not has_streak:
                            live_hit = classify_banner_text(str(ocr_text))
                            has_streak = live_hit is not None and int(live_hit.tier or 0) >= 1
                    except Exception:
                        has_streak = False
                strong_pos = False
                if not has_streak:
                    try:
                        from mlbb_banner_ref_match import match_positive_owner_reference

                        pos = match_positive_owner_reference(frame)
                        if pos is not None:
                            ps, pr, _pp = pos
                            pr_l = str(pr or "").lower()
                            if float(ps) >= float(
                                os.environ.get("MLBB_BANNER_POS_LIVE_MIN_SIM", "0.55")
                            ) and any(
                                k in pr_l
                                for k in (
                                    "double",
                                    "triple",
                                    "savage",
                                    "maniac",
                                    "legendary",
                                )
                            ):
                                # Only trust pos over no_banner if it wins or ties closely.
                                if float(ps) + 0.01 >= float(_score):
                                    strong_pos = True
                    except Exception:
                        strong_pos = False
                if not has_streak and not strong_pos:
                    return False, f"neg_ref:{reason}"
    except Exception:
        pass

    def _ocr_has_kill_streak(text: str) -> bool:
        if not text:
            return False
        try:
            from mlbb_kill_banner import _KILL_STREAK_HINT_RE, classify_banner_text

            if _KILL_STREAK_HINT_RE.search(str(text)):
                return True
            hit = classify_banner_text(str(text))
            return hit is not None and int(hit.tier or 0) >= 1
        except Exception:
            return False

    killer = extract_killer_portrait_patch(frame)
    hud = extract_hud_hero_portrait_patch(frame)
    if killer is not None and hud is not None:
        hud_score = patch_pair_score(killer, hud)
        # Discover uses a softer floor so candidates reach montage/presend;
        # solo send still gated by live DOUBLE+ (no fake live_streak_tier).
        min_hud = float(os.environ.get("MLBB_BANNER_OWN_HUD_MIN_SIM", "0.22"))
        if os.environ.get("MLBB_BANNER_OWN_KILL_PHASE", "") == "discover":
            min_hud = float(
                os.environ.get("MLBB_BANNER_DISCOVER_OWN_HUD_MIN_SIM", "0.20")
            )
        if hud_score >= min_hud:
            # HUD portrait match alone is not a kill — OZLs/Zy8 mid frames
            # matched HUD on empty banner ROI and shipped jog clips.
            if os.environ.get("MLBB_OWN_KILL_HUD_REQUIRE_EVIDENCE", "1") == "1":
                has_streak = _ocr_has_kill_streak(str(ocr_text or ""))
                has_pos = False
                if not has_streak:
                    try:
                        from mlbb_banner_ref_match import match_positive_owner_reference

                        has_pos = match_positive_owner_reference(frame) is not None
                    except Exception:
                        has_pos = False
                if not has_streak and not has_pos:
                    return False, f"hud_no_kill_evidence:{hud_score:.3f}"
            return True, f"hud_killer_ok:{hud_score:.3f}"
        # Usable HUD that does not match killer → ally/enemy kill banner.
        return False, f"hud_killer_mismatch:{hud_score:.3f}"

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
            any_match = best_hero_match(frame)
            if any_match and any_match[0] != played:
                return False, f"killer_not_played:{any_match[0]}!={played}"
            return False, "killer_icon_miss"
        return True, f"killer_icon_ok:{played}:{match[1]:.3f}"

    # Cannot verify own kill — reject rather than ship ally/enemy banners.
    if killer is None:
        return False, "killer_portrait_miss"
    if hud is None and not (icon_library_ready() and played):
        return False, "own_kill_unverifiable"
    return False, "own_kill_unverifiable"
