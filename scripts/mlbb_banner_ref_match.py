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
    env = os.environ.get("MLBB_BANNER_REF_ROOT", "").strip()
    if env:
        return Path(env)
    # Prefer the content-bot data bank — NEVER /usr/local/data (thin/poisoned copy).
    repo = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if repo:
        cand = Path(repo) / "data" / "mlbb_kill_banners"
        if cand.exists():
            return cand
    cand = Path("/root/content_bot_ml/data/mlbb_kill_banners")
    if cand.exists():
        return cand
    return _repo_root() / "data" / "mlbb_kill_banners"


def _ref_match_enabled() -> bool:
    return os.environ.get("MLBB_BANNER_REF_MATCH", "1") == "1"


def _ref_min_sim() -> float:
    return float(os.environ.get("MLBB_BANNER_REF_MIN_SIM", "0.38"))


def _vod_crop_ref_enabled() -> bool:
    """VOD crops are error-prone; off by default for generic ref matching."""
    return os.environ.get("MLBB_BANNER_REF_VOD_CROP", "0") == "1"


def _vod_crop_ref_min_sim() -> float:
    return float(os.environ.get("MLBB_BANNER_REF_VOD_CROP_MIN_SIM", "0.62"))


def _generic_ref_sources() -> frozenset[str]:
    return frozenset({"wiki", "glob"})


def _neg_ref_min_sim() -> float:
    return float(os.environ.get("MLBB_BANNER_NEG_REF_MIN_SIM", "0.42"))


def _pos_ref_min_sim() -> float:
    # Explicit env wins over stale calibration profile.
    if "MLBB_BANNER_POS_REF_MIN_SIM" in os.environ:
        return float(os.environ["MLBB_BANNER_POS_REF_MIN_SIM"])
    prof_path = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb")) / "banner_calibration_profile.json"
    if prof_path.exists():
        try:
            prof = json.loads(prof_path.read_text(encoding="utf-8"))
            th = prof.get("thresholds") or {}
            if "MLBB_BANNER_POS_REF_MIN_SIM" in th:
                return float(th["MLBB_BANNER_POS_REF_MIN_SIM"])
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    # Higher default — weak matches were shipping gold-HUD junk as "savage".
    return 0.45


def _pos_ref_min_sim_for_reason(reason: str) -> float:
    base = _pos_ref_min_sim()
    r = str(reason or "").lower()
    # Generic own-kill crops are noisier than labeled double/triple/savage.
    if "own_kill" in r or "not_enemy" in r:
        return max(base, float(os.environ.get("MLBB_BANNER_POS_OWN_KILL_MIN_SIM", "0.50")))
    if "savage" in r:
        return max(base, float(os.environ.get("MLBB_BANNER_POS_SAVAGE_MIN_SIM", "0.48")))
    return base


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


def patch_hist_similarity(patch_a, patch_b) -> float:
    """Hue-robust histogram correlation (weak alone — gold HUD is everywhere)."""
    import cv2

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


def patch_edge_similarity(patch_a, patch_b) -> float:
    """
    Structural similarity of kill-banner silhouettes.
    HSV alone matches empty gold HUD; edges/NCC catch ornate banner shapes.
    """
    import cv2
    import numpy as np

    if patch_a is None or patch_b is None:
        return 0.0
    try:
        a = cv2.resize(patch_a, (160, 48))
        b = cv2.resize(patch_b, (160, 48))
        ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
        gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
        ea = cv2.Canny(ga, 60, 160)
        eb = cv2.Canny(gb, 60, 160)
        # Normalized cross-correlation on edge maps
        ea_f = ea.astype(np.float32)
        eb_f = eb.astype(np.float32)
        ea_f -= float(ea_f.mean())
        eb_f -= float(eb_f.mean())
        denom = float(np.linalg.norm(ea_f) * np.linalg.norm(eb_f))
        if denom < 1e-6:
            return 0.0
        ncc = float(np.sum(ea_f * eb_f) / denom)
        # Also compare gradient magnitude histograms (orientation-light)
        ga_f = cv2.Sobel(ga, cv2.CV_32F, 1, 0, ksize=3)
        gb_f = cv2.Sobel(gb, cv2.CV_32F, 1, 0, ksize=3)
        ha = cv2.calcHist([np.abs(ga_f)], [0], None, [32], [0, 512])
        hb = cv2.calcHist([np.abs(gb_f)], [0], None, [32], [0, 512])
        cv2.normalize(ha, ha)
        cv2.normalize(hb, hb)
        hist = float(cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL))
        return max(0.0, 0.65 * max(0.0, ncc) + 0.35 * max(0.0, hist))
    except Exception:
        return 0.0


def patch_similarity(patch_a, patch_b) -> float:
    """
    Combined similarity: structural edges dominate, HSV is a weak prior.
    Empty gold UI used to score 0.5+ on HSV alone — that is no longer enough.
    """
    hist = patch_hist_similarity(patch_a, patch_b)
    edge = patch_edge_similarity(patch_a, patch_b)
    # Edges carry kill-banner ornament; hist alone is capped.
    return float(0.70 * edge + 0.30 * max(0.0, min(1.0, hist)))


def _edge_min_for_match() -> float:
    return float(os.environ.get("MLBB_BANNER_EDGE_MIN_SIM", "0.28"))


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
                rows.append(
                    (
                        path,
                        str(row.get("name") or Path(rel).stem),
                        str(row.get("source") or ""),
                        str(row.get("tier_hint") or "unknown"),
                    )
                )
        except (json.JSONDecodeError, OSError):
            pass
    if not rows:
        # Prefer wiki/ only — never rglob owner_cal/negative or ui_template as positives.
        wiki = root / "wiki"
        search_roots = [wiki] if wiki.is_dir() else [root]
        for base in search_roots:
            for path in sorted(base.rglob("*.png")):
                rel = str(path.relative_to(root)).replace("\\", "/")
                if "owner_cal/" in rel or "ui_template" in rel or "manifest" in path.name:
                    continue
                parent = path.parent.name
                tier = parent if parent in {"double", "triple", "maniac", "savage", "unknown", "wiki"} else "unknown"
                source = "wiki" if rel.startswith("wiki/") else ("vod_crop" if rel.startswith("vod_crops/") else "glob")
                rows.append((str(path), path.stem, source, tier))
    return tuple(rows)


@lru_cache(maxsize=1)
def _load_negative_ref_rows() -> tuple[tuple[str, str, str], ...]:
    rows: list[tuple[str, str, str]] = []
    root = banner_ref_root() / "owner_cal" / "negative"
    if not root.exists():
        return tuple()
    excluded = {
        reason.strip()
        for reason in os.environ.get(
            "MLBB_BANNER_NEG_EXCLUDE_REASONS",
            "wrong_hero",
        ).split(",")
        if reason.strip()
    }
    for path in sorted(root.rglob("*.png")):
        reason = path.parent.name if path.parent != root else "unknown"
        if reason in excluded:
            continue
        rows.append((str(path), reason, reason))
    return tuple(rows)


def _tier_from_owner_reason(reason: str) -> int:
    r = str(reason or "").lower()
    if "savage" in r:
        return 5
    if "maniac" in r or "legendary" in r:
        return 4
    if "triple" in r:
        return 3
    if "double" in r:
        return 2
    # Owner-marked own kill / not-enemy — treat as double-min for live send.
    if "own_kill" in r or "not_enemy" in r:
        return 2
    return _tier_from_hint(r)


@lru_cache(maxsize=1)
def _load_positive_owner_ref_rows() -> tuple[tuple[str, str, str], ...]:
    rows: list[tuple[str, str, str]] = []
    root = banner_ref_root() / "owner_cal" / "positive"
    # UI-menu /banner screenshots (ownerphoto_* or ui_template/) are bad live matchers —
    # they match combat FX / chat. Prefer in-game crops from button labeling + vod_crops.
    allow_ui = os.environ.get("MLBB_BANNER_POS_ALLOW_UI_TEMPLATES", "0") == "1"
    if root.exists():
        for path in sorted(root.rglob("*.png")):
            reason = path.parent.name if path.parent != root else "unknown"
            if reason == "ui_template" and not allow_ui:
                continue
            if (not allow_ui) and path.name.startswith("ownerphoto_"):
                continue
            rows.append((str(path), reason, reason))
    # Labeled VOD crops can poison live match (one bad crop matches all combat).
    # Off by default — owner_cal positives are the trusted bank.
    if os.environ.get("MLBB_BANNER_POS_INCLUDE_VOD_CROPS", "0") == "1":
        vod_root = banner_ref_root() / "vod_crops"
        for tier_name in ("savage", "maniac", "triple", "double"):
            d = vod_root / tier_name
            if not d.is_dir():
                continue
            for path in sorted(d.glob("*.png")):
                rows.append((str(path), tier_name, f"vod_crop:{tier_name}"))
    prefer = (
        "savage",
        "savage_tier",
        "maniac",
        "triple",
        "double",
        "double_triple",
        "own_kill_good",
        "not_enemy_kill",
    )
    rows.sort(key=lambda r: prefer.index(r[1]) if r[1] in prefer else 50)
    return tuple(rows)


def clear_banner_ref_cache() -> None:
    _load_ref_rows.cache_clear()
    _load_negative_ref_rows.cache_clear()
    _load_positive_owner_ref_rows.cache_clear()


def _best_owner_match(
    frame,
    rows: tuple[tuple[str, str, str], ...],
    *,
    min_combined: float,
    require_edge: bool,
) -> tuple[float, str, str] | None:
    patch = extract_banner_zone_patch(frame)
    if patch is None:
        return None
    if not rows:
        return None
    best: tuple[float, str, str] | None = None
    edge_floor = _edge_min_for_match()
    for path, reason, _tag in rows:
        ref = _ref_patch_cached(path)
        if ref is None:
            continue
        edge = patch_edge_similarity(patch, ref)
        if require_edge and edge < edge_floor:
            continue
        score = patch_similarity(patch, ref)
        if best is None or score > best[0]:
            best = (score, reason, path)
    if best is None or best[0] < min_combined:
        return None
    return best


def match_positive_owner_reference(frame) -> tuple[float, str, str] | None:
    """Return (score, reason, path) if frame matches owner-labeled good banner crop."""
    if not _pos_ref_match_enabled():
        return None
    rows = _load_positive_owner_ref_rows()
    patch = extract_banner_zone_patch(frame)
    if patch is None or not rows:
        return None
    best: tuple[float, str, str] | None = None
    edge_floor = _edge_min_for_match()
    require_edge = os.environ.get("MLBB_BANNER_POS_REQUIRE_EDGE", "1") == "1"
    for path, reason, _tag in rows:
        ref = _ref_patch_cached(path)
        if ref is None:
            continue
        edge = patch_edge_similarity(patch, ref)
        if require_edge and edge < edge_floor:
            continue
        score = patch_similarity(patch, ref)
        if score < _pos_ref_min_sim_for_reason(reason):
            continue
        if best is None or score > best[0]:
            best = (score, reason, path)
    return best


def match_positive_owner_reference_strict(frame) -> tuple[float, str, str] | None:
    """Higher bar for teach/presend — needs clear structural banner match."""
    if not _pos_ref_match_enabled():
        return None
    min_sim = max(_pos_ref_min_sim(), float(os.environ.get("MLBB_BANNER_POS_STRICT_MIN_SIM", "0.42")))
    old_edge = os.environ.get("MLBB_BANNER_EDGE_MIN_SIM")
    os.environ["MLBB_BANNER_EDGE_MIN_SIM"] = os.environ.get("MLBB_BANNER_POS_STRICT_EDGE_MIN", "0.34")
    try:
        return _best_owner_match(
            frame,
            _load_positive_owner_ref_rows(),
            min_combined=min_sim,
            require_edge=True,
        )
    finally:
        if old_edge is None:
            os.environ.pop("MLBB_BANNER_EDGE_MIN_SIM", None)
        else:
            os.environ["MLBB_BANNER_EDGE_MIN_SIM"] = old_edge


def match_negative_banner_reference(frame) -> tuple[float, str, str] | None:
    """Return (score, reason, path) if frame matches owner-labeled negative crop."""
    if not _neg_ref_match_enabled():
        return None
    # Negatives must also clear an edge floor so generic HUD doesn't veto OCR kills.
    require_edge = os.environ.get("MLBB_BANNER_NEG_REQUIRE_EDGE", "1") == "1"
    return _best_owner_match(
        frame,
        _load_negative_ref_rows(),
        min_combined=_neg_ref_min_sim(),
        require_edge=require_edge,
    )


@lru_cache(maxsize=256)
def _ref_patch_cached(path: str):
    import cv2

    img = cv2.imread(path)
    return _prep_ref_patch(img)


def _ref_min_sim_for_source(source: str) -> float:
    if source == "vod_crop":
        return _vod_crop_ref_min_sim()
    return _ref_min_sim()


def _row_allowed_for_generic_match(source: str) -> bool:
    """Generic HUD match uses wiki frames only; VOD/owner crops use dedicated paths."""
    if source in _generic_ref_sources():
        return True
    if source == "vod_crop" and _vod_crop_ref_enabled():
        return True
    if source.startswith("owner_cal:"):
        return False
    return False


def match_banner_reference(
    frame,
    *,
    ignore_negative: bool = False,
) -> tuple[float, str, str, int] | None:
    """
    Return (score, ref_name, source, tier) for best reference match, or None.
    """
    if not _ref_match_enabled():
        return None
    if not ignore_negative:
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
        if not _row_allowed_for_generic_match(source):
            continue
        ref = _ref_patch_cached(path)
        if ref is None:
            continue
        score = patch_similarity(patch, ref)
        tier = _tier_from_hint(tier_hint)
        min_sim = _ref_min_sim_for_source(source)
        if score < min_sim:
            continue
        if best is None or score > best[0]:
            best = (score, name, source, tier)
    if best is None:
        return None
    return best


def classify_banner_reference(sec: float, frame, *, vod: Path | None = None) -> KillBannerHit | None:
    """
    Match frame against the screenshot bank.

    Ref alone is not enough — weak hist/edge matches fire on ordinary combat
    (shipping junk). Require a strong gold announce flash, high structural
    similarity, and prefer labeled double/triple/savage over generic own_kill.
    """
    import cv2  # noqa: F401 — keep import path stable for callers

    labels = {5: "savage", 4: "maniac", 3: "triple", 2: "double", 1: "single"}
    # Color gate: real kill banners flash gold/white; farming combat does not.
    try:
        from mlbb_kill_banner import (
            _announce_color_score,
            _color_min_score,
            _discover_active,
            _ref_classify_min_tier,
        )
    except Exception:
        return None
    color = float(_announce_color_score(frame))
    ref_mul = float(os.environ.get("MLBB_BANNER_REF_COLOR_MUL", "1.25"))
    if _discover_active():
        ref_mul = min(ref_mul, float(os.environ.get("MLBB_BANNER_DISCOVER_REF_COLOR_MUL", "0.75")))
    need_color = float(_color_min_score()) * ref_mul
    if color < need_color:
        return None

    min_tier_needed = _ref_classify_min_tier()
    neg = match_negative_banner_reference(frame)
    pos = match_positive_owner_reference(frame)
    if pos is not None:
        score, reason, path = pos
        tag = str(path)
        # Poisoned vod_crops must never ship alone.
        if "vod_crops" in tag.replace("\\", "/") and os.environ.get(
            "MLBB_BANNER_ALLOW_VOD_CROP_REF", "0"
        ) != "1":
            pos = None
        elif neg is not None and float(neg[0]) >= float(score) - float(
            os.environ.get("MLBB_BANNER_NEG_POS_MARGIN", "0.06")
        ):
            # Close neg (not_kill/enemy) beats a weak pos — 3lO0 farm FP was
            # pos=0.51 vs not_kill=0.49 with the old 0.02 margin.
            pos = None
        else:
            # Structural bar: junk FP on WTJrJ was ~0.55 hist-edge on every frame.
            min_live = float(os.environ.get("MLBB_BANNER_POS_LIVE_MIN_SIM", "0.62"))
            if _discover_active():
                min_live = min(
                    min_live,
                    float(os.environ.get("MLBB_BANNER_DISCOVER_POS_LIVE_MIN_SIM", "0.55")),
                )
            reason_l = str(reason or "").lower()
            if "own_kill" in reason_l or "not_enemy" in reason_l:
                own_min = float(os.environ.get("MLBB_BANNER_POS_OWN_KILL_MIN_SIM", "0.68"))
                if _discover_active():
                    own_min = min(own_min, float(os.environ.get("MLBB_BANNER_DISCOVER_OWN_KILL_MIN_SIM", "0.55")))
                min_live = max(min_live, own_min)
            if float(score) < min_live:
                pos = None
            else:
                tier = _tier_from_owner_reason(reason)
                if tier >= min_tier_needed:
                    return KillBannerHit(
                        sec=round(sec, 2),
                        tier=tier,
                        label=labels.get(tier, "double"),
                        text=(
                            f"owner_pos={reason} sim={score:.3f} "
                            f"color={color:.3f} path={Path(path).name}"
                        ),
                        source="ref",
                    )
    # Wiki/generic refs are even noisier — only with very strong color.
    wiki_color_mul = float(os.environ.get("MLBB_BANNER_WIKI_COLOR_MUL", "1.6"))
    if _discover_active():
        wiki_color_mul = min(wiki_color_mul, float(os.environ.get("MLBB_BANNER_DISCOVER_WIKI_COLOR_MUL", "1.1")))
    if color < float(_color_min_score()) * wiki_color_mul:
        return None
    row = match_banner_reference(frame, ignore_negative=True)
    if row is None or neg is not None:
        return None
    score, name, source, tier = row
    if tier < min_tier_needed:
        return None
    wiki_min = float(os.environ.get("MLBB_BANNER_WIKI_MIN_SIM", "0.55"))
    if _discover_active():
        wiki_min = min(wiki_min, float(os.environ.get("MLBB_BANNER_DISCOVER_WIKI_MIN_SIM", "0.50")))
    if float(score) < wiki_min:
        return None
    return KillBannerHit(
        sec=round(sec, 2),
        tier=tier,
        label=labels.get(tier, "double"),
        text=f"ref={name} sim={score:.3f} src={source} color={color:.3f}",
        source="ref",
    )
