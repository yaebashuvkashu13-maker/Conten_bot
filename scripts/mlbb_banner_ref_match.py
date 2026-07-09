#!/usr/bin/env python3
"""Visual match gameplay banner zone against reference bank (wiki + VOD crops)."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from mlbb_kill_banner import KillBannerHit, _min_tier


def _repo_root() -> Path:
    env = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent.parent
    if (root / "data").exists():
        return root
    return Path("/root/content_bot_ml")


def banner_ref_root() -> Path:
    return Path(os.environ.get("MLBB_BANNER_REF_ROOT", str(_repo_root() / "data" / "mlbb_kill_banners")))


def _ref_match_enabled() -> bool:
    return os.environ.get("MLBB_BANNER_REF_MATCH", "1") == "1"


def _ref_min_sim() -> float:
    return float(os.environ.get("MLBB_BANNER_REF_MIN_SIM", "0.38"))


def _neg_ref_min_sim() -> float:
    return float(os.environ.get("MLBB_BANNER_NEG_REF_MIN_SIM", "0.42"))


def _pos_ref_min_sim() -> float:
    prof_path = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb")) / "banner_calibration_profile.json"
    if prof_path.exists():
        try:
            prof = json.loads(prof_path.read_text(encoding="utf-8"))
            th = prof.get("thresholds") or {}
            if "MLBB_BANNER_POS_REF_MIN_SIM" in th:
                return float(th["MLBB_BANNER_POS_REF_MIN_SIM"])
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    return float(os.environ.get("MLBB_BANNER_POS_REF_MIN_SIM", "0.36"))


def _pos_ref_match_enabled() -> bool:
    return os.environ.get("MLBB_BANNER_POS_REF_MATCH", "1") == "1"


def _neg_ref_match_enabled() -> bool:
    return os.environ.get("MLBB_BANNER_NEG_REF_MATCH", "1") == "1"


def _tier_from_hint(hint: str) -> int:
    return {
        "savage": 5,
        "legendary": 5,
        "maniac": 4,
        "triple": 3,
        "double": 2,
        "single": 1,
    }.get(str(hint or "").lower(), 2)


def extract_banner_zone_patch(frame) -> object | None:
    import cv2

    if frame is None:
        return None
    h, w = frame.shape[:2]
    if h < 80 or w < 160:
        return None
    y0, y1 = int(h * 0.02), int(h * 0.30)
    x0, x1 = int(w * 0.15), int(w * 0.85)
    patch = frame[y0:y1, x0:x1]
    if patch.size == 0:
        return None
    return cv2.resize(patch, (160, 48))


def _prep_ref_patch(img) -> object | None:
    import cv2

    if img is None:
        return None
    if img.shape[0] < img.shape[1] * 0.8:
        return cv2.resize(img, (160, 48))
    # Wiki previews are square frames — stretch to in-game banner aspect.
    return cv2.resize(img, (160, 48))


def patch_similarity(patch_a, patch_b) -> float:
    """Hue-robust histogram correlation, same idea as POV portrait match."""
    import cv2
    import numpy as np

    if patch_a is None or patch_b is None:
        return 0.0
    try:
        a = cv2.cvtColor(patch_a, cv2.COLOR_BGR2HSV)
        b = cv2.cvtColor(patch_b, cv2.COLOR_BGR2HSV)
        hist_a = cv2.calcHist([a], [0, 1], None, [24, 16], [0, 180, 0, 256])
        hist_b = cv2.calcHist([b], [0, 1], None, [24, 16], [0, 180, 0, 256])
        cv2.normalize(hist_a, hist_a)
        cv2.normalize(hist_b, hist_b)
        return float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL))
    except Exception:
        return 0.0


@lru_cache(maxsize=1)
def _load_ref_rows() -> tuple[tuple[str, str, str, str], ...]:
    rows: list[tuple[str, str, str, str]] = []
    root = banner_ref_root()
    manifest = root / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for row in data.get("refs") or []:
                rel = str(row.get("path") or "")
                if not rel:
                    continue
                path = str(root / rel) if not Path(rel).is_absolute() else rel
                rows.append((path, str(row.get("name") or Path(rel).stem), str(row.get("source") or ""), str(row.get("tier_hint") or "unknown")))
        except (json.JSONDecodeError, OSError):
            pass
    if not rows:
        for path in sorted(root.rglob("*.png")):
            if "manifest" in path.name:
                continue
            tier = path.parent.name if path.parent.name in {"double", "triple", "maniac", "savage", "unknown", "wiki"} else "unknown"
            rows.append((str(path), path.stem, "glob", tier))
    return tuple(rows)


@lru_cache(maxsize=1)
def _load_negative_ref_rows() -> tuple[tuple[str, str, str], ...]:
    rows: list[tuple[str, str, str]] = []
    root = banner_ref_root() / "owner_cal" / "negative"
    if not root.exists():
        return tuple()
    for path in sorted(root.rglob("*.png")):
        reason = path.parent.name if path.parent != root else "unknown"
        rows.append((str(path), reason, reason))
    return tuple(rows)


@lru_cache(maxsize=1)
def _load_positive_owner_ref_rows() -> tuple[tuple[str, str, str], ...]:
    rows: list[tuple[str, str, str]] = []
    root = banner_ref_root() / "owner_cal" / "positive"
    if not root.exists():
        return tuple()
    for path in sorted(root.rglob("*.png")):
        reason = path.parent.name if path.parent != root else "unknown"
        rows.append((str(path), reason, reason))
    return tuple(rows)


def clear_banner_ref_cache() -> None:
    _load_ref_rows.cache_clear()
    _load_negative_ref_rows.cache_clear()
    _load_positive_owner_ref_rows.cache_clear()


def match_positive_owner_reference(frame) -> tuple[float, str, str] | None:
    """Return (score, reason, path) if frame matches owner-labeled good banner crop."""
    if not _pos_ref_match_enabled():
        return None
    patch = extract_banner_zone_patch(frame)
    if patch is None:
        return None
    rows = _load_positive_owner_ref_rows()
    if not rows:
        return None
    best: tuple[float, str, str] | None = None
    for path, reason, _tag in rows:
        ref = _ref_patch_cached(path)
        if ref is None:
            continue
        score = patch_similarity(patch, ref)
        if best is None or score > best[0]:
            best = (score, reason, path)
    if best is None or best[0] < _pos_ref_min_sim():
        return None
    return best


def match_negative_banner_reference(frame) -> tuple[float, str, str] | None:
    """Return (score, reason, path) if frame matches owner-labeled negative crop."""
    if not _neg_ref_match_enabled():
        return None
    patch = extract_banner_zone_patch(frame)
    if patch is None:
        return None
    rows = _load_negative_ref_rows()
    if not rows:
        return None
    best: tuple[float, str, str] | None = None
    for path, reason, _tag in rows:
        ref = _ref_patch_cached(path)
        if ref is None:
            continue
        score = patch_similarity(patch, ref)
        if best is None or score > best[0]:
            best = (score, reason, path)
    if best is None or best[0] < _neg_ref_min_sim():
        return None
    return best


@lru_cache(maxsize=256)
def _ref_patch_cached(path: str):
    import cv2

    img = cv2.imread(path)
    return _prep_ref_patch(img)


def match_banner_reference(frame) -> tuple[float, str, str, int] | None:
    """
    Return (score, ref_name, source, tier) for best reference match, or None.
    """
    if not _ref_match_enabled():
        return None
    neg = match_negative_banner_reference(frame)
    if neg is not None:
        return None
    patch = extract_banner_zone_patch(frame)
    if patch is None:
        return None
    rows = _load_ref_rows()
    if not rows:
        return None
    best: tuple[float, str, str, int] | None = None
    for path, name, source, tier_hint in rows:
        ref = _ref_patch_cached(path)
        if ref is None:
            continue
        score = patch_similarity(patch, ref)
        tier = _tier_from_hint(tier_hint)
        if best is None or score > best[0]:
            best = (score, name, source, tier)
    if best is None or best[0] < _ref_min_sim():
        return None
    return best


def classify_banner_reference(sec: float, frame) -> KillBannerHit | None:
    row = match_banner_reference(frame)
    if row is None:
        return None
    score, name, source, tier = row
    if tier < _min_tier():
        return None
    return KillBannerHit(
        sec=round(sec, 2),
        tier=tier,
        label={5: "savage", 4: "maniac", 3: "triple", 2: "double", 1: "single"}.get(tier, "double"),
        text=f"ref={name} sim={score:.3f} src={source}",
        source="ref",
    )
