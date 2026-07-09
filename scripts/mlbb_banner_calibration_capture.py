#!/usr/bin/env python3
"""Capture MLBB banner calibration screenshots with HUD zone overlay."""

from __future__ import annotations

import os
from pathlib import Path

from mlbb_banner_calibration_store import _shots_root, check_id, upsert_check, vod_youtube_id


def _banner_zone_rect(frame) -> tuple[int, int, int, int]:
    h, w = frame.shape[:2]
    y0, y1 = int(h * 0.02), int(h * 0.30)
    x0, x1 = int(w * 0.15), int(w * 0.85)
    return x0, y0, x1, y1


def render_check_screenshot(
    vod: Path,
    sec: float,
    *,
    hit=None,
    out_dir: Path | None = None,
) -> tuple[Path, dict]:
    """
    Save JPEG with green rectangle on banner HUD zone.
    Returns (screenshot_path, metadata_row).
    """
    import cv2

    from gameplay_gate import _read_frame_at

    frame = _read_frame_at(vod, sec)
    if frame is None:
        raise RuntimeError(f"no_frame:{vod.name}@{sec}")

    vis = frame.copy()
    x0, y0, x1, y1 = _banner_zone_rect(frame)
    cv2.rectangle(vis, (x0, y0), (x1, y1), (40, 220, 40), 2)
    label_parts = [f"t={sec:.1f}s"]
    banner_tier = None
    banner_label = ""
    detected_text = ""
    if hit is not None:
        banner_tier = getattr(hit, "tier", None)
        banner_label = str(getattr(hit, "label", "") or "")
        detected_text = str(getattr(hit, "text", "") or "")[:80]
        src = str(getattr(hit, "source", "") or "")
        label_parts.append(f"{banner_label or 'banner'} tier={banner_tier} src={src}")
        if detected_text:
            label_parts.append(detected_text[:48])
    caption = " | ".join(label_parts)
    cv2.putText(
        vis,
        caption[:120],
        (max(8, x0), max(24, y0 - 8)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (40, 220, 40),
        2,
        cv2.LINE_AA,
    )

    root = out_dir or _shots_root()
    root.mkdir(parents=True, exist_ok=True)
    cid = check_id(vod, sec)
    dest = root / f"{cid}.jpg"
    cv2.imwrite(str(dest), vis, [int(cv2.IMWRITE_JPEG_QUALITY), 88])

    row = {
        "check_id": cid,
        "vod": str(vod),
        "vod_id": vod_youtube_id(vod),
        "sec": round(sec, 2),
        "screenshot": str(dest),
        "banner_tier": banner_tier,
        "banner_label": banner_label,
        "detected_text": detected_text,
        "banner_source": str(getattr(hit, "source", "") or "") if hit else "",
    }
    upsert_check(row)
    return dest, row


def discover_candidates(vod: Path, *, limit: int = 12) -> list:
    """Return banner hits worth sending for owner calibration."""
    from mlbb_kill_banner import KillBannerHit, discover_vod_kill_banners, find_banner_near_peak
    from mlbb_vod_dense_hints import audit_banner_hints

    min_tier = int(os.environ.get("MLBB_BANNER_CALIB_MIN_TIER", "1"))
    vid = vod_youtube_id(vod)
    hint_secs = audit_banner_hints(vid, min_tier=min_tier)
    hits: list = []
    if hint_secs:
        for sec in hint_secs[: max(limit * 2, limit)]:
            hit = find_banner_near_peak(vod, sec, quick=True)
            if hit is None:
                hit = KillBannerHit(
                    sec=round(sec, 2),
                    tier=max(min_tier, 3),
                    label="audit",
                    text="dense_audit_hint",
                    source="audit",
                )
            hits.append(hit)
    if not hits:
        max_sec = float(os.environ.get("MLBB_BANNER_CALIB_DISCOVER_MAX_SEC", "900"))
        prev = os.environ.get("MLBB_KILL_BANNER_DISCOVER_MAX_SEC")
        os.environ["MLBB_KILL_BANNER_DISCOVER_MAX_SEC"] = str(max_sec)
        try:
            hits = discover_vod_kill_banners(vod, min_tier=min_tier)
        finally:
            if prev is None:
                os.environ.pop("MLBB_KILL_BANNER_DISCOVER_MAX_SEC", None)
            else:
                os.environ["MLBB_KILL_BANNER_DISCOVER_MAX_SEC"] = prev
    if not hits:
        return []
    hits = sorted(hits, key=lambda h: (-int(h.tier), h.sec))
    picked: list = []
    used_midpoints: list[float] = []
    gap = float(os.environ.get("MLBB_BANNER_CALIB_MIN_GAP_SEC", "18"))
    for hit in hits:
        if any(abs(hit.sec - mid) < gap for mid in used_midpoints):
            continue
        picked.append(hit)
        used_midpoints.append(hit.sec)
        if len(picked) >= limit:
            break
    return picked
