#!/usr/bin/env python3
"""Diagnose why kill banners were missed at owner-confirmed timestamps."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

OWNER_MARKS: dict[str, list[int]] = {
    "opuealwWYA0": [49, 95],
    "1hezrcufqhc": [16, 539, 627],
    "tkQjwO3DIzA": [217, 221, 825, 829],
    "j3an3JmAhHI": [242, 277],
    "318oBNepZRY": [310, 490, 497, 500, 541, 634],
    "bez0hBUpH9A": [347, 386, 530],
}


def motion_peak_near(vod: Path, sec: float, *, radius: float = 30.0) -> float | None:
    from mlbb_fight_segment import _analysis_for
    from mlbb_kill_banner import _analysis_series

    analysis = _analysis_for(vod)
    win = float(analysis.get("window_seconds", 2.0))
    motion = _analysis_series(analysis, "center_motion")
    audio = _analysis_series(analysis, "audio")
    if not motion:
        return None
    i0 = max(0, int((sec - radius) / win))
    i1 = min(len(motion), int((sec + radius) / win) + 1)
    if i1 <= i0:
        return None
    best_i = max(
        range(i0, i1),
        key=lambda i: float(motion[i]) + float(audio[i] if i < len(audio) else 0) * 0.5,
    )
    return round(best_i * win + win * 0.5, 1)


def _vod_min_peak_sec(vod: Path | None = None) -> float:
    base = float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "420"))
    if vod is None:
        return base
    from smart_video_editor import ffprobe_duration

    dur = float(ffprobe_duration(vod) or 0.0)
    if dur <= 240:
        return min(base, 45.0)
    if dur <= 480:
        return min(base, 120.0)
    return base


def diagnose_mark(vod: Path, owner_sec: float) -> dict:
    from mlbb_kill_banner import (
        _adaptive_banner_scan_start,
        _analysis_series,
        classify_banner_text,
        find_banner_near_peak,
        scan_window,
    )
    from smart_video_editor import ffprobe_duration

    dur = float(ffprobe_duration(vod) or 0.0)
    row: dict = {"owner_sec": owner_sec, "vod_dur": round(dur, 1)}
    hits = scan_window(vod, max(0.0, owner_sec - 3), owner_sec + 4, focus_sec=owner_sec, deep=True)
    row["ocr_at_mark"] = [
        {"sec": h.sec, "tier": h.tier, "label": h.label, "text": h.text[:100], "src": h.source}
        for h in hits[:6]
    ]
    direct = find_banner_near_peak(vod, owner_sec)
    row["find_at_owner_sec"] = (
        None
        if not direct
        else {
            "sec": direct.sec,
            "tier": direct.tier,
            "label": direct.label,
            "text": direct.text[:100],
            "src": direct.source,
        }
    )
    mp = motion_peak_near(vod, owner_sec)
    row["motion_peak"] = mp
    row["peak_offset"] = round(abs((mp or owner_sec) - owner_sec), 1)
    min_peak = _vod_min_peak_sec(vod)
    row["adaptive_min_peak"] = min_peak
    row["blocked_by_min_peak"] = bool(mp is not None and mp < min_peak)
    row["banner_scan_start"] = _adaptive_banner_scan_start(vod, dur)
    if mp is not None:
        at_peak = find_banner_near_peak(vod, mp)
        row["find_at_motion_peak"] = (
            None
            if not at_peak
            else {"sec": at_peak.sec, "tier": at_peak.tier, "label": at_peak.label, "src": at_peak.source}
        )
    best = max(hits, key=lambda h: h.tier, default=None)
    if row.get("blocked_by_min_peak"):
        row["likely_miss"] = f"min_peak_sec={min_peak}"
    elif best and best.tier < 2:
        row["likely_miss"] = f"ocr_single_tier={best.tier}:{best.text[:60]}"
    elif not hits:
        row["likely_miss"] = "ocr_no_text"
    elif direct is None and mp is not None:
        row["likely_miss"] = f"peak_offset_{row['peak_offset']}s_no_banner_in_scan_window"
    elif direct and direct.tier >= 2:
        row["likely_miss"] = "should_detect_check_pipeline"
    else:
        row["likely_miss"] = "unknown"
    return row


def main() -> int:
    inbox = Path(os.environ.get("HIGHLIGHT_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
    out: list[dict] = []
    for vid, times in OWNER_MARKS.items():
        vod = inbox / f"yt_{vid}.mp4"
        if not vod.exists():
            out.append({"vid": vid, "error": "missing_vod"})
            continue
        for t in times:
            try:
                row = diagnose_mark(vod, float(t))
                row["vid"] = vid
                out.append(row)
            except Exception as exc:
                out.append({"vid": vid, "owner_sec": t, "error": str(exc)[:200]})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
