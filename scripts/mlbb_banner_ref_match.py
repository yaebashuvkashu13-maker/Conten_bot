#!/usr/bin/env python3
"""Visual match gameplay banner zone against wiki + owner-labeled screenshots.

Ship path:
  cyan tip → match owner_cal positives / owner photos / wiki
  → reject if closer to owner-labeled negatives (no_banner, enemy_kill, …)
  → source=ref only

Owner labeled hundreds of screenshots in banner_calibration_labels.json —
those are the ground truth, not generic heuristics.
"""

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


def owner_photo_root() -> Path:
    return Path(
        os.environ.get(
            "MLBB_BANNER_OWNER_PHOTOS",
            "/root/data/mlbb/banner_owner_photos",
        )
    )


def _ref_match_enabled() -> bool:
    return os.environ.get("MLBB_BANNER_REF_MATCH", "1") == "1"


def _profile_thresholds() -> dict:
    path = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb")) / "banner_calibration_profile.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        th = data.get("thresholds") or {}
        return th if isinstance(th, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _ref_min_sim() -> float:
    th = _profile_thresholds()
    if "MLBB_BANNER_POS_REF_MIN_SIM" in th:
        try:
            return float(th["MLBB_BANNER_POS_REF_MIN_SIM"])
        except (TypeError, ValueError):
            pass
    return float(os.environ.get("MLBB_BANNER_REF_MIN_SIM", "0.36"))


def _owner_min_sim() -> float:
    th = _profile_thresholds()
    # Profile POS_OWN_KILL_MIN_SIM=0.50 was tuned for old hist-only scores and
    # rejects real owner screenshots under edge-aware similarity (~0.43–0.55).
    raw = os.environ.get("MLBB_BANNER_OWNER_MIN_SIM")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    if "MLBB_BANNER_POS_OWN_KILL_MIN_SIM" in th:
        try:
            # Soft ceiling — never demand >0.45 for owner photos/crops.
            return min(0.45, float(th["MLBB_BANNER_POS_OWN_KILL_MIN_SIM"]))
        except (TypeError, ValueError):
            pass
    return 0.40


def _neg_wins(pos_score: float, neg_score: float, *, source: str) -> bool:
    """Reject only when negative clearly beats positive.

    Hue hist of top HUD is similar for kill banner vs plain HUD (no_banner),
    so a soft margin the other way killed 39/40 true owner goods.
    """
    if source in ("owner", "owner_cal"):
        gap = float(os.environ.get("MLBB_BANNER_OWNER_NEG_WIN_GAP", "0.10"))
        return neg_score > pos_score + gap
    gap = float(os.environ.get("MLBB_BANNER_WIKI_NEG_WIN_GAP", "0.03"))
    return neg_score > pos_score + gap


def _neg_min_sim() -> float:
    th = _profile_thresholds()
    if "MLBB_BANNER_NEG_REF_MIN_SIM" in th:
        try:
            return float(th["MLBB_BANNER_NEG_REF_MIN_SIM"])
        except (TypeError, ValueError):
            pass
    return float(os.environ.get("MLBB_BANNER_NEG_REF_MIN_SIM", "0.42"))


def _neg_enabled() -> bool:
    return os.environ.get("MLBB_BANNER_NEG_REF_MATCH", "1") == "1"


def _tier_from_hint(hint: str) -> int:
    return {
        "savage": 5,
        "legendary": 5,
        "maniac": 4,
        "triple": 3,
        "double": 2,
        "double_triple": 2,
        "own_kill_good": 2,
        "single": 1,
        "unknown": 2,
        "wiki": 2,
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
    # Already a wide banner crop from owner_cal sync.
    if img.shape[0] < img.shape[1] * 0.8:
        return cv2.resize(img, (160, 48))
    h, w = img.shape[:2]
    y0, y1 = int(h * 0.02), int(h * 0.32)
    x0, x1 = int(w * 0.12), int(w * 0.88)
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return cv2.resize(img, (160, 48))
    return cv2.resize(crop, (160, 48))


def patch_similarity(patch_a, patch_b) -> float:
    """Hue-robust histogram correlation + light edge agreement."""
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
        hist = float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL))
        # Edge correl catches banner letterforms better than hue alone.
        ga = cv2.cvtColor(patch_a, cv2.COLOR_BGR2GRAY)
        gb = cv2.cvtColor(patch_b, cv2.COLOR_BGR2GRAY)
        ea = cv2.Canny(ga, 60, 140)
        eb = cv2.Canny(gb, 60, 140)
        ea_f = ea.astype(np.float32).ravel()
        eb_f = eb.astype(np.float32).ravel()
        if float(ea_f.std()) < 1e-3 or float(eb_f.std()) < 1e-3:
            return hist
        edge = float(np.corrcoef(ea_f, eb_f)[0, 1])
        if edge != edge:  # NaN
            edge = 0.0
        return 0.72 * hist + 0.28 * edge
    except Exception:
        return 0.0


@lru_cache(maxsize=1)
def _load_ref_rows() -> tuple[tuple[str, str, str, str], ...]:
    """(path, name, source, tier_hint) — positives only."""
    rows: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    root = banner_ref_root()

    def _add(path: Path, name: str, source: str, tier: str) -> None:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen or not path.exists():
            return
        seen.add(key)
        rows.append((str(path), name, source, tier))

    # 1) Owner-cal positive crops (labeled screenshots) — strongest prior.
    pos_root = root / "owner_cal" / "positive"
    if pos_root.exists():
        for path in sorted(pos_root.rglob("*.png")):
            tier = path.parent.name if path.parent.name not in {"positive"} else "own_kill_good"
            _add(path, path.stem[:32], "owner_cal", tier)
        for path in sorted(pos_root.rglob("*.jpg")):
            tier = path.parent.name if path.parent.name not in {"positive"} else "own_kill_good"
            _add(path, path.stem[:32], "owner_cal", tier)

    # 2) Raw owner photos (confirmed kill banners).
    photos = owner_photo_root()
    if photos.exists() and os.environ.get("MLBB_BANNER_OWNER_REFS", "1") == "1":
        limit = max(40, int(os.environ.get("MLBB_BANNER_OWNER_PHOTO_LIMIT", "250")))
        for path in sorted(list(photos.glob("*.jpg")) + list(photos.glob("*.png")))[:limit]:
            _add(path, path.stem[:24], "owner", "double")

    # 3) Wiki / manifest refs.
    manifest = root / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for row in data.get("refs") or []:
                rel = str(row.get("path") or "")
                if not rel:
                    continue
                path = root / rel if not Path(rel).is_absolute() else Path(rel)
                _add(
                    path,
                    str(row.get("name") or path.stem),
                    str(row.get("source") or "wiki"),
                    str(row.get("tier_hint") or "unknown"),
                )
        except (json.JSONDecodeError, OSError):
            pass
    if not any(s == "wiki" or s == "glob" for _, _, s, _ in rows):
        for path in sorted((root / "wiki").glob("*.png")) if (root / "wiki").exists() else []:
            _add(path, path.stem, "wiki", "unknown")

    return tuple(rows)


@lru_cache(maxsize=1)
def _load_negative_ref_rows() -> tuple[tuple[str, str], ...]:
    """(path, reason)"""
    rows: list[tuple[str, str]] = []
    root = banner_ref_root() / "owner_cal" / "negative"
    if not root.exists():
        return tuple()
    limit = max(40, int(os.environ.get("MLBB_BANNER_NEG_REF_LIMIT", "220")))
    for path in sorted(list(root.rglob("*.png")) + list(root.rglob("*.jpg")))[:limit]:
        reason = path.parent.name if path.parent != root else "unknown"
        rows.append((str(path), reason))
    return tuple(rows)


def clear_banner_ref_cache() -> None:
    _load_ref_rows.cache_clear()
    _load_negative_ref_rows.cache_clear()
    _ref_patch_cached.cache_clear()


@lru_cache(maxsize=512)
def _ref_patch_cached(path: str):
    import cv2

    img = cv2.imread(path)
    return _prep_ref_patch(img)


def _best_sim(patch, rows: list[tuple], *, path_idx: int = 0) -> tuple[float, object] | None:
    best: tuple[float, object] | None = None
    for row in rows:
        path = row[path_idx]
        ref = _ref_patch_cached(path)
        if ref is None:
            continue
        score = patch_similarity(patch, ref)
        if best is None or score > best[0]:
            best = (score, row)
    return best


def match_banner_reference(frame) -> tuple[float, str, str, int] | None:
    """Return (score, ref_name, source, tier) for best reference match, or None."""
    if not _ref_match_enabled():
        return None
    patch = extract_banner_zone_patch(frame)
    if patch is None:
        return None
    rows = _load_ref_rows()
    if not rows:
        return None

    if os.environ.get("MLBB_BANNER_REF_MATCH_ALL", "0") != "1":
        cap = max(40, int(os.environ.get("MLBB_BANNER_REF_MATCH_CAP", "140")))
        owner_cal = [r for r in rows if r[2] == "owner_cal"]
        owner = [r for r in rows if r[2] == "owner"]
        wiki = [r for r in rows if r[2] not in ("owner_cal", "owner")]
        n_cal = min(len(owner_cal), max(40, cap * 2 // 3))
        n_own = min(len(owner), max(20, cap // 5))
        n_wiki = min(len(wiki), max(10, cap - n_cal - n_own))
        rows = tuple(owner_cal[:n_cal] + owner[:n_own] + wiki[:n_wiki])

    scored: list[tuple[float, str, str, int]] = []
    for path, name, source, tier_hint in rows:
        ref = _ref_patch_cached(path)
        if ref is None:
            continue
        score = patch_similarity(patch, ref)
        if source in ("owner", "owner_cal"):
            need = _owner_min_sim()
            if source == "owner_cal":
                need = min(need, float(os.environ.get("MLBB_BANNER_OWNER_CAL_MIN_SIM", "0.38")))
        else:
            need = _ref_min_sim()
        if score < need:
            continue
        scored.append((score, name, source, _tier_from_hint(tier_hint)))

    if not scored:
        return None
    scored.sort(key=lambda r: -r[0])
    best = scored[0]
    top_k = max(1, int(os.environ.get("MLBB_BANNER_POS_TOPK", "3")))
    pos_top = sum(s[0] for s in scored[:top_k]) / min(top_k, len(scored))

    # Negatives: compare top-k means — single best hist is too noisy on HUD chrome.
    if _neg_enabled():
        neg_rows = _load_negative_ref_rows()
        if neg_rows:
            if os.environ.get("MLBB_BANNER_REF_MATCH_ALL", "0") != "1":
                cap_n = max(30, int(os.environ.get("MLBB_BANNER_NEG_MATCH_CAP", "100")))
                by: dict[str, list] = {}
                for path, reason in neg_rows:
                    by.setdefault(reason, []).append((path, reason))
                picked: list[tuple[str, str]] = []
                while len(picked) < cap_n and any(by.values()):
                    for reason in list(by.keys()):
                        bucket = by.get(reason) or []
                        if not bucket:
                            continue
                        picked.append(bucket.pop(0))
                        if len(picked) >= cap_n:
                            break
                neg_rows = tuple(picked)
            neg_scores: list[float] = []
            for path, _reason in neg_rows:
                ref = _ref_patch_cached(path)
                if ref is None:
                    continue
                neg_scores.append(patch_similarity(patch, ref))
            if neg_scores:
                neg_scores.sort(reverse=True)
                neg_top = sum(neg_scores[:top_k]) / min(top_k, len(neg_scores))
                lead = float(os.environ.get("MLBB_BANNER_POS_NEG_LEAD", "0.04"))
                # Strong self-match (≥0.60): allow even if neg is close.
                if best[0] < 0.60 and pos_top < neg_top + lead:
                    return None
                if best[0] >= 0.60 and _neg_wins(best[0], neg_scores[0], source=best[2]):
                    return None

    return best


def match_negative_banner_reference(frame) -> tuple[float, str, str] | None:
    if not _neg_enabled():
        return None
    patch = extract_banner_zone_patch(frame)
    if patch is None:
        return None
    rows = _load_negative_ref_rows()
    if not rows:
        return None
    # Cap negatives too — no_banner dominates; keep mix of reasons.
    if os.environ.get("MLBB_BANNER_REF_MATCH_ALL", "0") != "1":
        cap = max(30, int(os.environ.get("MLBB_BANNER_NEG_MATCH_CAP", "100")))
        by: dict[str, list] = {}
        for path, reason in rows:
            by.setdefault(reason, []).append((path, reason))
        picked: list[tuple[str, str]] = []
        # Round-robin reasons so enemy_kill isn't drowned by no_banner.
        while len(picked) < cap and any(by.values()):
            for reason in list(by.keys()):
                bucket = by.get(reason) or []
                if not bucket:
                    continue
                picked.append(bucket.pop(0))
                if len(picked) >= cap:
                    break
        rows = tuple(picked)

    early = float(os.environ.get("MLBB_BANNER_NEG_EARLY_ACCEPT", "0.70"))
    best: tuple[float, str, str] | None = None
    for path, reason in rows:
        ref = _ref_patch_cached(path)
        if ref is None:
            continue
        score = patch_similarity(patch, ref)
        if best is None or score > best[0]:
            best = (score, reason, path)
            if best[0] >= early:
                break
    if best is None or best[0] < _neg_min_sim():
        return None
    return best


def classify_banner_reference(sec: float, frame) -> KillBannerHit | None:
    row = match_banner_reference(frame)
    if row is None:
        return None
    score, name, source, tier = row
    if tier < _min_tier():
        if source in ("owner", "owner_cal", "wiki", "glob") and tier < 2:
            tier = 2
        if tier < _min_tier():
            return None
    return KillBannerHit(
        sec=round(sec, 2),
        tier=max(tier, 2),
        label={5: "savage", 4: "maniac", 3: "triple", 2: "double", 1: "single"}.get(
            max(tier, 2), "double"
        ),
        text=f"ref={name} sim={score:.3f} src={source}",
        source="ref",
    )
