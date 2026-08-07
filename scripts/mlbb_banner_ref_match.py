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
    # Env always wins — profile POS_OWN_KILL_MIN_SIM=0.50 was for old hist scores.
    raw = os.environ.get("MLBB_BANNER_OWNER_MIN_SIM")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    th = _profile_thresholds()
    if "MLBB_BANNER_POS_OWN_KILL_MIN_SIM" in th:
        try:
            return min(0.42, float(th["MLBB_BANNER_POS_OWN_KILL_MIN_SIM"]))
        except (TypeError, ValueError):
            pass
    return 0.36


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
    # Must match mlbb_banner_owner_cal_sync._crop_banner_zone — self-match
    # on owner labels collapses if live crop is tighter than training crops.
    y0, y1 = int(h * 0.02), int(h * 0.32)
    x0, x1 = int(w * 0.12), int(w * 0.88)
    patch = frame[y0:y1, x0:x1]
    if patch.size == 0:
        return None
    return cv2.resize(patch, (160, 48))


def _prep_ref_patch(img) -> object | None:
    import cv2

    if img is None:
        return None
    # Already a wide banner crop from owner_cal sync (320x96 etc).
    if img.shape[0] < img.shape[1] * 0.8:
        return cv2.resize(img, (160, 48))
    # Full frame / photo — same geometry as extract_banner_zone_patch.
    h, w = img.shape[:2]
    y0, y1 = int(h * 0.02), int(h * 0.32)
    x0, x1 = int(w * 0.12), int(w * 0.88)
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return cv2.resize(img, (160, 48))
    return cv2.resize(crop, (160, 48))


def patch_similarity(patch_a, patch_b) -> float:
    """Hue hist + edge letterforms + cyan-mask agreement.

    Owner kill banners share a cyan horizontal band + white text edges.
    Hue alone confuses them with plain top-HUD (no_banner labels).
    """
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
        ga = cv2.cvtColor(patch_a, cv2.COLOR_BGR2GRAY)
        gb = cv2.cvtColor(patch_b, cv2.COLOR_BGR2GRAY)
        ea = cv2.Canny(ga, 60, 140).astype(np.float32)
        eb = cv2.Canny(gb, 60, 140).astype(np.float32)
        ea_f = ea.ravel()
        eb_f = eb.ravel()
        if float(ea_f.std()) < 1e-3 or float(eb_f.std()) < 1e-3:
            edge = 0.0
        else:
            edge = float(np.corrcoef(ea_f, eb_f)[0, 1])
            if edge != edge:
                edge = 0.0
        ca = cv2.inRange(a, (75, 40, 80), (130, 255, 255)).astype(np.float32)
        cb = cv2.inRange(b, (75, 40, 80), (130, 255, 255)).astype(np.float32)
        ca_f = ca.ravel()
        cb_f = cb.ravel()
        if float(ca_f.std()) < 1e-3 or float(cb_f.std()) < 1e-3:
            cyan = 0.0
        else:
            cyan = float(np.corrcoef(ca_f, cb_f)[0, 1])
            if cyan != cyan:
                cyan = 0.0
        # Edges+cyan carry kill-letterform shape; hist is soft prior.
        return 0.48 * hist + 0.32 * edge + 0.20 * cyan
    except Exception:
        return 0.0


def banner_structure_score(frame) -> float:
    """0..1 — cyan horizontal kill-band strength in top HUD (owner goods ~0.55+)."""
    import cv2
    import numpy as np

    patch = extract_banner_zone_patch(frame)
    if patch is None:
        return 0.0
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    cyan = cv2.inRange(hsv, (75, 40, 80), (130, 255, 255))
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    edge = float((cv2.Canny(gray, 60, 140) > 0).mean())
    cyan_r = float((cyan > 0).mean())
    row = (cyan > 0).mean(axis=1)
    band = float(row.max()) if row.size else 0.0
    hh, ww = cyan.shape
    center = float((cyan[int(hh * 0.2) : int(hh * 0.8), int(ww * 0.25) : int(ww * 0.75)] > 0).mean())
    # Tuned on owner labels: own_kill cyan~0.39 band~0.66; no_banner cyan~0.26 band~0.49
    raw = 0.35 * cyan_r + 0.35 * band + 0.20 * center + 0.10 * min(1.0, edge / 0.22)
    return float(max(0.0, min(1.0, raw)))


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
    _load_logit_model.cache_clear()


@lru_cache(maxsize=512)
def _ref_patch_cached(path: str):
    import cv2

    img = cv2.imread(path)
    return _prep_ref_patch(img)


@lru_cache(maxsize=1)
def _load_logit_model() -> tuple[object, float] | None:
    """Owner-label logistic weights trained by mlbb_banner_owner_cal_sync."""
    path = banner_ref_root() / "owner_cal" / "banner_logit.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        import numpy as np

        w = np.asarray(data.get("w") or [], dtype=np.float64)
        b = float(data.get("b") or 0.0)
        thr = float(data.get("thr") or os.environ.get("MLBB_BANNER_LOGIT_THR", "0.45"))
        if w.size < 8:
            return None
        return (w, b), thr
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def _patch_logit_features(patch) -> object | None:
    import cv2
    import numpy as np

    if patch is None:
        return None
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 12], [0, 180, 0, 256]).flatten()
    hist = hist / (hist.sum() + 1e-6)
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    edge_map = cv2.Canny(gray, 60, 140)
    edge = float((edge_map > 0).mean())
    cyan = cv2.inRange(hsv, (75, 40, 80), (130, 255, 255))
    gold = cv2.inRange(hsv, (15, 60, 100), (40, 255, 255))
    white = cv2.inRange(hsv, (0, 0, 180), (180, 50, 255))
    hh, ww = patch.shape[:2]
    cy0, cy1 = int(hh * 0.2), int(hh * 0.8)
    cx0, cx1 = int(ww * 0.25), int(ww * 0.75)
    center = cyan[cy0:cy1, cx0:cx1]
    row = (cyan > 0).mean(axis=1)
    band = float(row.max()) if row.size else 0.0
    # Horizontal edge energy — kill text is a wide letter strip.
    edge_row = (edge_map > 0).mean(axis=1)
    edge_band = float(edge_row.max()) if edge_row.size else 0.0
    return np.concatenate(
        [
            hist,
            [
                edge,
                float((cyan > 0).mean()),
                float((gold > 0).mean()),
                float((white > 0).mean()),
                float((center > 0).mean()) if center.size else 0.0,
                float(gray.mean() / 255.0),
                float(gray.std() / 255.0),
                band,
                edge_band,
            ],
        ]
    )


def owner_logit_score(frame) -> float | None:
    """P(kill-banner) from owner-label logistic model, or None if unavailable."""
    if os.environ.get("MLBB_BANNER_OWNER_LOGIT", "1") != "1":
        return None
    model = _load_logit_model()
    if model is None:
        return None
    (w, b), _thr = model
    patch = extract_banner_zone_patch(frame)
    feat = _patch_logit_features(patch)
    if feat is None:
        return None
    import numpy as np

    if feat.shape[0] != w.shape[0]:
        return None
    z = float(feat @ w + b)
    return float(1.0 / (1.0 + np.exp(-max(-20.0, min(20.0, z)))))


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

    early = float(os.environ.get("MLBB_BANNER_REF_EARLY_ACCEPT", "0.68"))
    best: tuple[float, str, str, int] | None = None
    weak: tuple[float, str, str, int] | None = None
    floor = float(os.environ.get("MLBB_BANNER_FLOOR_SIM", "0.28"))
    for path, name, source, tier_hint in rows:
        ref = _ref_patch_cached(path)
        if ref is None:
            continue
        score = patch_similarity(patch, ref)
        tier = _tier_from_hint(tier_hint)
        if source in ("owner", "owner_cal") and (weak is None or score > weak[0]):
            weak = (score, name, source, tier)
        if source in ("owner", "owner_cal"):
            need = _owner_min_sim()
            if source == "owner_cal":
                need = min(need, float(os.environ.get("MLBB_BANNER_OWNER_CAL_MIN_SIM", "0.36")))
        else:
            need = _ref_min_sim()
        if score < need:
            continue
        if best is None or score > best[0]:
            best = (score, name, source, tier)
            if best[0] >= early:
                break

    # Soft floor: keep best weak owner hit for logit+structure gate.
    if best is None and weak is not None and weak[0] >= floor:
        best = weak

    if best is None:
        return None

    struct = banner_structure_score(frame)
    struct_thr = float(os.environ.get("MLBB_BANNER_STRUCT_THR", "0.48"))

    # Strong visual self-match to owner crop — trust it when cyan-band is present.
    strong = float(os.environ.get("MLBB_BANNER_STRONG_SIM", "0.55"))
    if best[0] >= strong and best[2] in ("owner", "owner_cal") and struct >= struct_thr * 0.80:
        return best

    # Weak hits: reject when clearly closer to owner-labeled no_banner/enemy.
    if best[0] < strong and _neg_enabled() and os.environ.get("MLBB_BANNER_WEAK_NEG_LEAD", "1") == "1":
        neg = match_negative_banner_reference(frame)
        lead = float(os.environ.get("MLBB_BANNER_POS_NEG_LEAD", "0.03"))
        if neg is not None and best[0] < neg[0] + lead:
            return None

    # Owner-label logistic is the main no_banner filter (hist neg-gate killed goods).
    model = _load_logit_model()
    if model is not None and os.environ.get("MLBB_BANNER_OWNER_LOGIT", "1") == "1":
        _wb, thr = model
        thr = float(os.environ.get("MLBB_BANNER_LOGIT_THR", str(thr)))
        soft = float(os.environ.get("MLBB_BANNER_LOGIT_SOFT_THR", str(max(0.28, thr - 0.10))))
        prob = owner_logit_score(frame)
        if prob is None:
            return None
        # Floor-only hits must clear both logit and structure (no free FP).
        cal_min = float(os.environ.get("MLBB_BANNER_OWNER_CAL_MIN_SIM", "0.36"))
        if best[0] < cal_min:
            if prob >= thr and struct >= struct_thr:
                return best
            return None
        if prob >= thr and struct >= struct_thr * 0.90:
            return best
        if prob >= thr and best[0] >= float(os.environ.get("MLBB_BANNER_SOFT_SIM", "0.42")) and struct >= struct_thr * 0.85:
            return best
        if (
            best[2] in ("owner", "owner_cal")
            and best[0] >= float(os.environ.get("MLBB_BANNER_SOFT_SIM", "0.42"))
            and struct >= struct_thr
            and prob >= soft
        ):
            return best
        return None
    if _neg_enabled():
        neg = match_negative_banner_reference(frame)
        if neg is not None and _neg_wins(best[0], neg[0], source=best[2]):
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
