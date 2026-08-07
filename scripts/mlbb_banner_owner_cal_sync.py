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

    meta = {
        "updated_from": str(labels_path),
        "positive": report["positive"],
        "negative": report["negative"],
        "owner_photos": report["owner_photos"],
        "by_reason": report["by_reason"],
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
