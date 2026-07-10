#!/usr/bin/env python3
"""Store for MLBB kill-banner screenshot calibration (owner button labels)."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from mlbb_banner_calibration_reasons import (
    NEGATIVE_REASONS,
    POSITIVE_REASONS,
    REASON_CODES,
    TIER_FOR_REASON,
    reason_label,
)


def _repo_root() -> Path:
    env = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent.parent
    if root.name == "bin" or str(root) == "/usr/local":
        return Path("/root/content_bot_ml")
    return root


REPO = _repo_root()


def _data_mlbb() -> Path:
    return Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))


def _shots_root() -> Path:
    return Path(os.environ.get("MLBB_BANNER_CALIB_SHOTS", "/root/datasets/mlbb/banner_calibration"))


def _index_path() -> Path:
    return Path(os.environ.get("MLBB_BANNER_CALIB_INDEX", str(_data_mlbb() / "banner_calibration_index.json")))


def _labels_path() -> Path:
    return Path(os.environ.get("MLBB_BANNER_CALIB_LABELS", str(_data_mlbb() / "banner_calibration_labels.json")))


def _sent_path() -> Path:
    return Path(os.environ.get("MLBB_BANNER_CALIB_SENT", str(_data_mlbb() / "banner_calibration_sent.json")))


def calibration_target() -> int:
    return int(os.environ.get("MLBB_BANNER_CALIB_TARGET", "50"))


def _read_json(path: Path, default: dict | list) -> dict | list:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def vod_youtube_id(path: Path | str) -> str:
    stem = Path(path).stem
    if stem.startswith("yt_") and len(stem) >= 14:
        return stem[3:14]
    match = re.search(r"(?:^|_)([A-Za-z0-9_-]{11})$", stem)
    if match:
        return match.group(1)
    match = re.search(r"([A-Za-z0-9_-]{11})", stem)
    return match.group(1) if match else stem[:24]


def check_id(vod_path: Path | str, sec: float) -> str:
    return f"{vod_youtube_id(vod_path)}_{int(round(sec))}"


def load_index() -> dict:
    data = _read_json(_index_path(), {"checks": [], "updated_at": ""})
    if not isinstance(data, dict):
        return {"checks": [], "updated_at": ""}
    data.setdefault("checks", [])
    return data


def save_index(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(_index_path(), data)


def remove_check_from_index(check_id_str: str) -> bool:
    cid = check_id_str.strip()
    if not cid:
        return False
    data = load_index()
    rows: list[dict] = data.get("checks", [])
    kept = [row for row in rows if str(row.get("check_id", "")) != cid]
    if len(kept) == len(rows):
        return False
    data["checks"] = kept
    save_index(data)
    return True


def upsert_check(row: dict) -> None:
    data = load_index()
    rows: list[dict] = data["checks"]
    cid = str(row.get("check_id", ""))
    replaced = False
    for i, existing in enumerate(rows):
        if existing.get("check_id") == cid:
            rows[i] = {**existing, **row}
            replaced = True
            break
    if not replaced:
        rows.append(row)
    save_index(data)


def find_check(check_id_str: str) -> dict | None:
    cid = check_id_str.strip()
    for row in load_index().get("checks", []):
        if row.get("check_id") == cid:
            return row
    shot = _shots_root() / f"{cid}.jpg"
    if shot.exists():
        return {"check_id": cid, "screenshot": str(shot)}
    return None


def load_labels() -> dict:
    data = _read_json(_labels_path(), {"labels": [], "updated_at": ""})
    if not isinstance(data, dict):
        return {"labels": [], "updated_at": ""}
    data.setdefault("labels", [])
    return data


def save_labels(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(_labels_path(), data)


def labeled_ids() -> dict[str, str]:
    out: dict[str, str] = {}
    for row in load_labels().get("labels", []):
        cid = str(row.get("check_id", ""))
        reason = str(row.get("reason", ""))
        if cid and reason:
            out[cid] = reason
    return out


def load_sent() -> set[str]:
    raw = _read_json(_sent_path(), {"sent_ids": []})
    if not isinstance(raw, dict):
        return set()
    return set(str(x) for x in raw.get("sent_ids", []))


def mark_sent(ids: list[str]) -> None:
    sent = load_sent()
    sent.update(str(x) for x in ids if x)
    _write_json(
        _sent_path(),
        {"sent_ids": sorted(sent), "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
    )


def _tier_for_row(reason: str, row: dict) -> str:
    if reason in TIER_FOR_REASON:
        hint = TIER_FOR_REASON[reason]
        if hint != "unknown":
            return hint
    detected = str(row.get("banner_label") or row.get("banner_tier_label") or "").lower()
    tier_map = {"savage": "savage", "legendary": "savage", "maniac": "maniac", "triple": "triple", "double": "double"}
    if detected in tier_map:
        return tier_map[detected]
    tier = row.get("banner_tier")
    if isinstance(tier, int):
        return {5: "savage", 4: "maniac", 3: "triple", 2: "double", 1: "single"}.get(tier, "unknown")
    return "unknown"


def _banner_ref_root() -> Path:
    return Path(os.environ.get("MLBB_BANNER_REF_ROOT", str(_repo_root() / "data" / "mlbb_kill_banners")))


def purge_vod_crops_for_row(row: dict) -> list[str]:
    """Remove VOD crop refs that a negative owner label proved wrong."""
    removed: list[str] = []
    root = _banner_ref_root() / "vod_crops"
    if not root.exists():
        return removed
    cid = str(row.get("check_id", "")).strip()
    vod = Path(str(row.get("vod", "")))
    sec = int(float(row.get("sec", 0)))
    vid = ""
    if vod.exists():
        vid = vod_youtube_id(vod).lower()
    needles = {cid.lower(), f"{vid}_{sec}s" if vid else ""}
    needles.discard("")
    for path in list(root.rglob("*.png")):
        stem = path.stem.lower()
        if any(n and n in stem for n in needles):
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                pass
    return removed


def purge_positive_crops_for_check(check_id_str: str) -> list[str]:
    """Drop stale owner-positive crops when owner re-labels as negative."""
    removed: list[str] = []
    root = _banner_ref_root() / "owner_cal" / "positive"
    if not root.exists():
        return removed
    cid = check_id_str.strip()
    for path in list(root.rglob(f"{cid}.png")):
        try:
            path.unlink()
            removed.append(str(path))
        except OSError:
            pass
    return removed


def _save_ref_crop(row: dict, reason: str) -> Path | None:
    from mlbb_banner_ref_ingest import crop_from_vod, extract_banner_crop

    vod = Path(str(row.get("vod", "")))
    sec = float(row.get("sec", 0))
    cid = str(row.get("check_id", ""))
    if not vod.exists():
        return None

    if reason in POSITIVE_REASONS:
        tier = _tier_for_row(reason, row)
        dest = crop_from_vod(vod, sec, tier=tier, video_id=vod_youtube_id(vod))
        if dest is None:
            return None
        owner_dir = _banner_ref_root() / "owner_cal" / "positive" / reason
        owner_dir.mkdir(parents=True, exist_ok=True)
        owner_copy = owner_dir / f"{cid}.png"
        shutil.copy2(dest, owner_copy)
        return owner_copy

    if reason in NEGATIVE_REASONS:
        import cv2
        from gameplay_gate import _read_frame_at

        frame = _read_frame_at(vod, sec)
        patch = extract_banner_crop(frame) if frame is not None else None
        if patch is None:
            shot = Path(str(row.get("screenshot", "")))
            if shot.exists():
                frame = cv2.imread(str(shot))
                patch = extract_banner_crop(frame)
        if patch is None:
            return None
        owner_dir = _banner_ref_root() / "owner_cal" / "negative" / reason
        owner_dir.mkdir(parents=True, exist_ok=True)
        dest = owner_dir / f"{cid}.png"
        cv2.imwrite(str(dest), patch)
        return dest
    return None


def _refresh_ref_manifest() -> None:
    try:
        from mlbb_banner_ref_ingest import write_manifest
        from mlbb_banner_ref_match import clear_banner_ref_cache

        write_manifest()
        clear_banner_ref_cache()
    except Exception:
        pass


def _append_owner_time_label(row: dict, reason: str) -> None:
    try:
        from mlbb_vod_segment_store import append_owner_label_json

        vid = vod_youtube_id(str(row.get("vod", "")))
        sec = float(row.get("sec", 0))
        if reason in POSITIVE_REASONS:
            append_owner_label_json(vid, sec, "good", note=reason, source="banner_calibration")
        elif reason in NEGATIVE_REASONS:
            append_owner_label_json(vid, sec, "bad", note=reason, source="banner_calibration")
    except Exception:
        pass


def sync_learning_after_label() -> None:
    """Apply owner button press to live ref bank + profile immediately."""
    _refresh_ref_manifest()
    try:
        from mlbb_banner_ref_match import clear_banner_ref_cache
        from mlbb_banner_calibration_apply import write_profile

        clear_banner_ref_cache()
        write_profile()
    except Exception:
        pass


def apply_owner_label(
    check_id_str: str,
    reason: str,
    *,
    by_chat: str = "",
) -> tuple[bool, str]:
    if reason not in REASON_CODES:
        return False, f"unknown_reason:{reason}"
    row = find_check(check_id_str)
    if not row:
        return False, f"unknown_check:{check_id_str}"

    crop_path = _save_ref_crop(row, reason)
    purged: list[str] = []
    if reason in NEGATIVE_REASONS:
        purged.extend(purge_vod_crops_for_row({**row, "check_id": check_id_str}))
        purged.extend(purge_positive_crops_for_check(check_id_str))
    entry = {
        "check_id": check_id_str,
        "reason": reason,
        "reason_label": reason_label(reason),
        "vod": row.get("vod", ""),
        "sec": row.get("sec", 0),
        "banner_tier": row.get("banner_tier"),
        "banner_label": row.get("banner_label", ""),
        "detected_text": row.get("detected_text", ""),
        "screenshot": row.get("screenshot", ""),
        "crop_path": str(crop_path) if crop_path else "",
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "by_chat": by_chat,
    }
    labels = load_labels()
    labels["labels"] = [x for x in labels.get("labels", []) if x.get("check_id") != check_id_str]
    labels["labels"].append(entry)
    save_labels(labels)

    _append_owner_time_label(row, reason)
    if reason in NEGATIVE_REASONS:
        remove_check_from_index(check_id_str)
    sync_learning_after_label()
    return True, reason


def stats() -> dict:
    labels = load_labels().get("labels", [])
    labeled = labeled_ids()
    sent = load_sent()
    target = calibration_target()
    by_reason: dict[str, int] = {}
    for row in labels:
        r = str(row.get("reason", ""))
        by_reason[r] = by_reason.get(r, 0) + 1
    return {
        "target": target,
        "labeled": len(labeled),
        "sent": len(sent),
        "pending_owner": max(0, len(sent) - len(labeled)),
        "remaining_to_target": max(0, target - len(labeled)),
        "by_reason": by_reason,
        "index_total": len(load_index().get("checks", [])),
    }


def pending_for_send(*, limit: int = 10) -> list[dict]:
    """Checks registered but not yet sent to owner."""
    labeled = labeled_ids()
    sent = load_sent()
    out: list[dict] = []
    for row in load_index().get("checks", []):
        cid = str(row.get("check_id", ""))
        if not cid or cid in sent or cid in labeled:
            continue
        shot = Path(str(row.get("screenshot", "")))
        if not shot.exists():
            shot = _shots_root() / f"{cid}.jpg"
        if not shot.exists():
            continue
        out.append({**row, "screenshot": str(shot)})
        if len(out) >= limit:
            break
    return out
