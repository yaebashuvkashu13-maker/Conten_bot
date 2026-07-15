#!/usr/bin/env python3
"""Ingest owner Telegram screenshots into the kill-banner reference bank."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path


REASON_FROM_CAPTION: list[tuple[str, str]] = [
    (r"savage|legendary|легенд", "savage_tier"),
    (r"maniac|маньяк|triple|triple.?kill|трой", "double_triple"),
    (r"double|дабл|double.?kill", "double_triple"),
    (r"own.?kill|свой.?kill|свой.?килл|ok\b", "own_kill_good"),
    # Interface / gallery banner frames without kill text → visual positive anchors
    (r"ui|interface|интерф|галер|hud|шаблон|баннер|banner", "own_kill_good"),
    (r"no.?banner|нет.?бан|пуст", "no_banner"),
    (r"not.?kill|не.?килл", "not_kill"),
    (r"enemy|чужой|противник", "enemy_kill"),
    (r"wrong.?hero|не.?тот", "wrong_hero"),
]


def _repo_root() -> Path:
    env = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if env:
        return Path(env)
    # Installed copy lives in /usr/local/bin — do not treat /usr/local as the repo.
    here = Path(__file__).resolve()
    if here.parent.name == "bin" and (Path("/root/content_bot_ml") / "scripts").is_dir():
        return Path("/root/content_bot_ml")
    return here.parent.parent


def banner_ref_root() -> Path:
    env = os.environ.get("MLBB_BANNER_REF_ROOT", "").strip()
    if env:
        return Path(env)
    return _repo_root() / "data" / "mlbb_kill_banners"


def inbox_dir() -> Path:
    root = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    path = Path(os.environ.get("MLBB_BANNER_OWNER_PHOTO_INBOX", str(root / "banner_owner_photos")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def reason_from_caption(caption: str) -> str | None:
    text = (caption or "").strip().lower()
    if not text:
        return None
    for pattern, reason in REASON_FROM_CAPTION:
        if re.search(pattern, text, flags=re.I):
            return reason
    return None


def default_positive_reason() -> str:
    # UI template shots without Double text still learn banner look — not no_banner.
    return os.environ.get("MLBB_BANNER_OWNER_PHOTO_DEFAULT_REASON", "own_kill_good")


def _photo_id(path: Path) -> str:
    digest = hashlib.sha1(path.read_bytes()[: 256 * 1024]).hexdigest()[:12]
    return f"ownerphoto_{digest}"


def ingest_owner_banner_photo(
    image_path: Path,
    *,
    reason: str | None = None,
    caption: str = "",
) -> dict:
    """
    Crop banner zone from an owner screenshot and store under owner_cal.
    Returns status dict for Telegram reply.
    """
    import cv2

    from mlbb_banner_calibration_reasons import NEGATIVE_REASONS, POSITIVE_REASONS
    from mlbb_banner_ref_ingest import extract_banner_crop, write_manifest
    from mlbb_banner_ref_match import clear_banner_ref_cache

    path = Path(image_path)
    if not path.exists():
        return {"ok": False, "error": "file_missing"}

    resolved = reason or reason_from_caption(caption) or default_positive_reason()
    if resolved not in POSITIVE_REASONS and resolved not in NEGATIVE_REASONS:
        return {"ok": False, "error": f"bad_reason:{resolved}"}

    frame = cv2.imread(str(path))
    if frame is None:
        return {"ok": False, "error": "unreadable_image"}

    patch = extract_banner_crop(frame)
    if patch is None:
        h, w = frame.shape[:2]
        # fallback: upper-mid crop typical for kill banners
        y0, y1 = int(h * 0.02), int(h * 0.32)
        x0, x1 = int(w * 0.12), int(w * 0.88)
        patch = frame[y0:y1, x0:x1]
        if patch.size == 0:
            return {"ok": False, "error": "empty_crop"}

    cid = _photo_id(path)
    bucket = "positive" if resolved in POSITIVE_REASONS else "negative"
    dest_dir = banner_ref_root() / "owner_cal" / bucket / resolved
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{cid}.png"
    cv2.imwrite(str(dest), patch)

    archive = inbox_dir() / f"{cid}_{resolved}{path.suffix.lower() or '.jpg'}"
    if not archive.exists():
        archive.write_bytes(path.read_bytes())

    labels_path = Path(
        os.environ.get(
            "MLBB_BANNER_CALIB_LABELS",
            str(Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb")) / "banner_calibration_labels.json"),
        )
    )
    payload = {"labels": [], "updated_at": ""}
    if labels_path.exists():
        try:
            payload = json.loads(labels_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = {"labels": []}
    labels = payload.setdefault("labels", [])
    labels.append(
        {
            "check_id": cid,
            "reason": resolved,
            "source": "owner_photo",
            "crop_path": str(dest),
            "screenshot": str(archive),
            "caption": (caption or "")[:120],
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    payload["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    labels_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        write_manifest()
        clear_banner_ref_cache()
    except Exception:
        pass

    try:
        from mlbb_banner_calibration_apply import write_profile

        write_profile()
    except Exception:
        pass

    pos_n = len(list((banner_ref_root() / "owner_cal" / "positive").rglob("*.png")))
    neg_n = len(list((banner_ref_root() / "owner_cal" / "negative").rglob("*.png")))
    return {
        "ok": True,
        "reason": resolved,
        "crop": str(dest),
        "check_id": cid,
        "positive_crops": pos_n,
        "negative_crops": neg_n,
    }
