#!/usr/bin/env python3
"""Rebuild ref bank + profile from owner banner-calibration labels."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_banner_calibration_reasons import NEGATIVE_REASONS, POSITIVE_REASONS
from mlbb_banner_calibration_store import (
    _banner_ref_root,
    _save_ref_crop,
    apply_owner_label,
    load_labels,
    stats,
)


def _profile_path() -> Path:
    return Path(
        os.environ.get(
            "MLBB_BANNER_CALIB_PROFILE",
            str(Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb")) / "banner_calibration_profile.json"),
        )
    )


def rebuild_crops_from_labels(*, force: bool = False) -> dict:
    """Ensure every labeled check has a crop in owner_cal/."""
    labels = load_labels().get("labels", [])
    out = {"total": len(labels), "crops_written": 0, "skipped": 0, "errors": 0}
    for row in labels:
        cid = str(row.get("check_id", ""))
        reason = str(row.get("reason", ""))
        if not cid or not reason:
            out["skipped"] += 1
            continue
        crop = Path(str(row.get("crop_path", "")))
        if crop.exists() and not force:
            out["skipped"] += 1
            continue
        from mlbb_banner_calibration_store import find_check

        check = find_check(cid) or row
        saved = _save_ref_crop(check, reason)
        if saved:
            out["crops_written"] += 1
            row["crop_path"] = str(saved)
        else:
            out["errors"] += 1
    return out


def write_profile() -> dict:
    st = stats()
    by_reason = dict(st.get("by_reason") or {})
    pos = sum(by_reason.get(r, 0) for r in POSITIVE_REASONS)
    neg = sum(by_reason.get(r, 0) for r in NEGATIVE_REASONS)
    root = _banner_ref_root()
    pos_files = len(list((root / "owner_cal" / "positive").rglob("*.png"))) if (root / "owner_cal" / "positive").exists() else 0
    neg_files = len(list((root / "owner_cal" / "negative").rglob("*.png"))) if (root / "owner_cal" / "negative").exists() else 0

    # Tune thresholds from label volume
    neg_sim = 0.42
    if neg_files >= 40:
        neg_sim = 0.40
    elif neg_files >= 20:
        neg_sim = 0.41
    pos_sim = 0.36
    if pos_files >= 8:
        pos_sim = 0.38

    profile = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "labeled": st.get("labeled", 0),
        "target": st.get("target", 50),
        "by_reason": by_reason,
        "positive_labels": pos,
        "negative_labels": neg,
        "positive_crops": pos_files,
        "negative_crops": neg_files,
        "gate_active": st.get("labeled", 0) >= int(os.environ.get("MLBB_BANNER_OWNER_GATE_MIN_LABELS", "20")),
        "thresholds": {
            "MLBB_BANNER_NEG_REF_MIN_SIM": neg_sim,
            "MLBB_BANNER_POS_REF_MIN_SIM": pos_sim,
            "MLBB_BANNER_OWNER_GATE": "1",
            "MLBB_BANNER_NEG_REF_MATCH": "1",
            "MLBB_BANNER_REF_MATCH": "1",
        },
    }
    path = _profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    return profile


def sync_owner_time_labels() -> int:
    """Push all banner-cal labels into mobile_legends_owner_labels.json."""
    from mlbb_vod_segment_store import append_owner_label_json, vod_youtube_id

    added = 0
    for row in load_labels().get("labels", []):
        reason = str(row.get("reason", ""))
        vod = str(row.get("vod", ""))
        if not vod:
            continue
        sec = float(row.get("sec", 0))
        vid = vod_youtube_id(vod)
        if reason in POSITIVE_REASONS:
            append_owner_label_json(vid, sec, "good", note=reason, source="banner_calibration_apply")
            added += 1
        elif reason in NEGATIVE_REASONS:
            append_owner_label_json(vid, sec, "bad", note=reason, source="banner_calibration_apply")
            added += 1
    return added


def apply_env_thresholds(profile: dict) -> list[str]:
    """Return recommended env lines (caller may patch .video_bot.env)."""
    th = profile.get("thresholds") or {}
    return [f"{k}={v}" for k, v in th.items()]


def main() -> int:
    crop_report = rebuild_crops_from_labels()
    try:
        from mlbb_banner_ref_ingest import write_manifest

        manifest = write_manifest()
    except Exception as exc:
        manifest = {"error": str(exc)}

    try:
        from mlbb_banner_ref_match import clear_banner_ref_cache

        clear_banner_ref_cache()
    except Exception:
        pass

    profile = write_profile()
    owner_sync = sync_owner_time_labels()
    env_lines = apply_env_thresholds(profile)

    print(
        json.dumps(
            {
                "crops": crop_report,
                "manifest_refs": manifest.get("count") if isinstance(manifest, dict) else manifest,
                "profile": profile,
                "owner_time_labels_synced": owner_sync,
                "env_apply": env_lines,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
