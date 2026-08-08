#!/usr/bin/env python3
"""
Rebuild owner_cal positive/negative banner crops from owner Telegram labels.

Sources (VPS):
  /root/data/mlbb/banner_calibration_labels.json
  /root/datasets/mlbb/banner_calibration/*.jpg
  /root/data/mlbb/banner_owner_photos/*.jpg

Writes:
  data/mlbb_kill_banners/owner_cal/positive/{own_kill_good,double_triple}/
  data/mlbb_kill_banners/owner_cal/negative/{no_banner,enemy_kill,not_kill,...}/

Safe to re-run. Does not touch wiki refs.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


POSITIVE_REASONS = frozenset({"own_kill_good", "double_triple"})
NEGATIVE_REASONS = frozenset(
    {"no_banner", "not_kill", "enemy_kill", "wrong_hero", "not_gameplay", "not_enemy_kill"}
)


def _repo_root() -> Path:
    env = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent.parent
    return root if (root / "data").exists() else Path("/root/content_bot_ml")


def _ref_root() -> Path:
    return Path(
        os.environ.get(
            "MLBB_BANNER_REF_ROOT",
            str(_repo_root() / "data" / "mlbb_kill_banners"),
        )
    )


def _labels_path() -> Path:
    return Path(
        os.environ.get(
            "MLBB_BANNER_CALIB_LABELS",
            "/root/data/mlbb/banner_calibration_labels.json",
        )
    )


def _owner_photos() -> Path:
    return Path(
        os.environ.get(
            "MLBB_BANNER_OWNER_PHOTOS",
            "/root/data/mlbb/banner_owner_photos",
        )
    )


def _crop_banner_zone(img):
    import cv2

    if img is None:
        return None
    h, w = img.shape[:2]
    if h < 80 or w < 160:
        return None
    # Full screenshots: top banner strip. Already-cropped wide patches: keep.
    if h < w * 0.55:
        return cv2.resize(img, (320, 96))
    y0, y1 = int(h * 0.02), int(h * 0.32)
    x0, x1 = int(w * 0.12), int(w * 0.88)
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    return cv2.resize(crop, (320, 96))


def _write_crop(src: Path, dest: Path) -> bool:
    import cv2

    img = cv2.imread(str(src))
    patch = _crop_banner_zone(img)
    if patch is None:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    return bool(cv2.imwrite(str(dest), patch))


def train_logit() -> dict:
    """Train logistic P(kill-banner) on owner labels + photos; write banner_logit.json."""
    import cv2
    import numpy as np

    from mlbb_banner_ref_match import extract_banner_zone_patch

    def feat(patch):
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
        center = cyan[int(hh * 0.2) : int(hh * 0.8), int(ww * 0.25) : int(ww * 0.75)]
        row = (cyan > 0).mean(axis=1)
        band = float(row.max()) if row.size else 0.0
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

    X: list = []
    y: list = []
    labels_path = _labels_path()
    if labels_path.exists():
        try:
            labels = list(json.loads(labels_path.read_text(encoding="utf-8")).get("labels") or [])
        except (json.JSONDecodeError, OSError):
            labels = []
        for row in labels:
            if not isinstance(row, dict):
                continue
            reason = str(row.get("reason") or "")
            if reason in POSITIVE_REASONS:
                label = 1.0
            elif reason in NEGATIVE_REASONS:
                label = 0.0
            else:
                continue
            shot = Path(str(row.get("screenshot") or ""))
            if not shot.exists():
                continue
            img = cv2.imread(str(shot))
            patch = extract_banner_zone_patch(img)
            if patch is None:
                continue
            X.append(feat(patch))
            y.append(label)

    photos = _owner_photos()
    if photos.exists():
        for path in sorted(photos.glob("*.jpg"))[:150]:
            img = cv2.imread(str(path))
            patch = extract_banner_zone_patch(img)
            if patch is None:
                continue
            X.append(feat(patch))
            y.append(1.0)

    if len(X) < 40 or sum(y) < 10 or (len(y) - sum(y)) < 10:
        return {"trained": False, "reason": "insufficient_labels", "n": len(X)}

    Xa = np.asarray(X, dtype=np.float64)
    ya = np.asarray(y, dtype=np.float64)
    w = np.zeros(Xa.shape[1], dtype=np.float64)
    b = 0.0
    lr = 0.35
    l2 = 0.02
    npos = max(1.0, float(ya.sum()))
    nneg = max(1.0, float(len(ya) - ya.sum()))
    for _ in range(800):
        z = Xa @ w + b
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -20, 20)))
        weights = np.where(ya > 0.5, nneg / npos, 1.0)
        err = (p - ya) * weights
        w -= lr * ((Xa.T @ err) / weights.sum() + l2 * w)
        b -= lr * (err.sum() / weights.sum())

    # Photos were appended last — evaluate threshold on label rows only.
    n_photos = 0
    if photos.exists():
        n_photos = min(150, len(list(photos.glob("*.jpg"))))
    n0 = max(40, len(ya) - n_photos)
    prob = 1.0 / (1.0 + np.exp(-np.clip(Xa[:n0] @ w + b, -20, 20)))
    yy = ya[:n0]
    # Prefer high recall on owner goods; precision secondary (live path also
    # needs owner_cal visual sim + structure).
    best = (0.0, 0.42, 0.0, 0.0)
    for thr in np.linspace(0.28, 0.58, 61):
        pred = prob >= thr
        tp = float(((pred == 1) & (yy == 1)).sum())
        fn = float(((pred == 0) & (yy == 1)).sum())
        fp = float(((pred == 1) & (yy == 0)).sum())
        rec = tp / (tp + fn + 1e-9)
        prec = tp / (tp + fp + 1e-9)
        f1 = 2 * prec * rec / (prec + rec + 1e-9)
        # Score: recall-first (≥0.88), then F1.
        score = f1 + (0.15 if rec >= 0.88 else 0.0) + (0.08 if rec >= 0.92 else 0.0)
        if score >= best[0]:
            best = (score, float(thr), rec, prec)

    out = _ref_root() / "owner_cal" / "banner_logit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "w": w.tolist(),
                "b": float(b),
                "thr": best[1],
                "f1": float(2 * best[3] * best[2] / (best[3] + best[2] + 1e-9)),
                "recall": best[2],
                "precision": best[3],
                "n": int(len(ya)),
                "pos": int(ya.sum()),
                "neg": int(len(ya) - ya.sum()),
                "feat": "hist16x12+edge+cyan+gold+white+center+meanstd+band+edge_band",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "trained": True,
        "thr": best[1],
        "f1": float(2 * best[3] * best[2] / (best[3] + best[2] + 1e-9)),
        "recall": best[2],
        "precision": best[3],
        "n": int(len(ya)),
        "path": str(out),
    }


def sync(*, wipe: bool = True) -> dict:
    import cv2  # noqa: F401 — fail fast if missing

    root = _ref_root() / "owner_cal"
    if wipe and root.exists():
        shutil.rmtree(root)
    pos_root = root / "positive"
    neg_root = root / "negative"
    pos_root.mkdir(parents=True, exist_ok=True)
    neg_root.mkdir(parents=True, exist_ok=True)

    report = {
        "positive": 0,
        "negative": 0,
        "owner_photos": 0,
        "skipped": 0,
        "by_reason": {},
    }

    labels_path = _labels_path()
    labels: list[dict] = []
    if labels_path.exists():
        try:
            raw = json.loads(labels_path.read_text(encoding="utf-8"))
            labels = list(raw.get("labels") or [])
        except (json.JSONDecodeError, OSError):
            labels = []

    for row in labels:
        if not isinstance(row, dict):
            continue
        reason = str(row.get("reason") or "").strip()
        shot = Path(str(row.get("screenshot") or ""))
        if not shot.exists():
            report["skipped"] += 1
            continue
        check_id = str(row.get("check_id") or shot.stem)
        if reason in POSITIVE_REASONS:
            dest = pos_root / reason / f"{check_id}.png"
            if _write_crop(shot, dest):
                report["positive"] += 1
                report["by_reason"][reason] = int(report["by_reason"].get(reason, 0)) + 1
            else:
                report["skipped"] += 1
        elif reason in NEGATIVE_REASONS:
            dest = neg_root / reason / f"{check_id}.png"
            if _write_crop(shot, dest):
                report["negative"] += 1
                report["by_reason"][reason] = int(report["by_reason"].get(reason, 0)) + 1
            else:
                report["skipped"] += 1
        else:
            report["skipped"] += 1

    # Owner Telegram photos are confirmed own-kill goods.
    photos = _owner_photos()
    if photos.exists() and os.environ.get("MLBB_BANNER_OWNER_REFS", "1") == "1":
        out = pos_root / "own_kill_good"
        out.mkdir(parents=True, exist_ok=True)
        for path in sorted(list(photos.glob("*.jpg")) + list(photos.glob("*.png"))):
            dest = out / f"photo_{path.stem[:40]}.png"
            if dest.exists():
                report["owner_photos"] += 1
                continue
            if _write_crop(path, dest):
                report["owner_photos"] += 1
                report["positive"] += 1
                report["by_reason"]["own_kill_good"] = (
                    int(report["by_reason"].get("own_kill_good", 0)) + 1
                )
            else:
                report["skipped"] += 1

    logit = train_logit()
    report["logit"] = logit

    meta = {
        "updated_from": str(labels_path),
        "positive": report["positive"],
        "negative": report["negative"],
        "owner_photos": report["owner_photos"],
        "by_reason": report["by_reason"],
        "logit": logit,
    }
    (root / "sync_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    report = sync(wipe=True)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
