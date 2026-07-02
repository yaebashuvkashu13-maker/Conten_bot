#!/usr/bin/env python3
"""Audit inbox VODs: highlight PASS peaks vs banner_reject at normalize stage."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlight_scorer import discover_highlight_candidates  # noqa: E402
from mlbb_fight_segment import clear_analysis_cache  # noqa: E402
from mlbb_vod_segment_feed import _normalize_clip, file_sha256  # noqa: E402
from strict_montage_direct import segment_key  # noqa: E402


def _ffprobe_duration(vod: Path) -> float:
    from smart_video_editor import ffprobe_duration

    return float(ffprobe_duration(vod))


def audit_vod(vod: Path, *, soften_level: int = 0) -> dict:
    from mlbb_vod_adaptive_gate import adaptive_env, overrides_for_level

    sig = file_sha256(vod)
    streak = 6 if soften_level >= 2 else (3 if soften_level >= 1 else 0)
    report: dict = {
        "vod": vod.name,
        "duration_sec": round(_ffprobe_duration(vod), 1),
        "soften_level": soften_level,
        "pass_peaks": 0,
        "motion_ok": 0,
        "banner_ok": 0,
        "banner_reject": 0,
        "short_clip": 0,
        "samples": [],
    }

    with adaptive_env(streak) as level:
        report["active_level"] = level
        report["env_motion_anchor"] = os.environ.get("MLBB_VOD_MOTION_ANCHOR_OK", "0")
        report["env_banner_required"] = os.environ.get("MLBB_KILL_BANNER_REQUIRED", "?")
        clear_analysis_cache()
        pool = discover_highlight_candidates(
            vod,
            "mobile_legends",
            used_keys=set(),
            segment_key_fn=segment_key,
            sig=sig,
            limit=int(os.environ.get("MLBB_VOD_PROBE_LIMIT", "24")),
        )
        for clip in pool:
            hm = clip.get("highlight_metrics") or {}
            if not hm.get("rule_pass") and not str(hm.get("pass_reason", "")).startswith("mlbb_fight"):
                continue
            peak = float(clip.get("start", 0))
            report["pass_peaks"] += 1
            norm = _normalize_clip(clip, vod)
            if norm.get("banner_reject"):
                report["banner_reject"] += 1
                if len(report["samples"]) < 8:
                    report["samples"].append(
                        {"peak": peak, "reject": norm.get("banner_reject"), "clip_score": hm.get("clip_score")}
                    )
                continue
            dur = float(norm.get("input_duration") or 0)
            if dur < float(os.environ.get("MLBB_FIGHT_MIN_SEC", "8")):
                report["short_clip"] += 1
                continue
            if norm.get("anchor") == "kill_banner":
                report["banner_ok"] += 1
            else:
                report["motion_ok"] += 1
            if len(report["samples"]) < 8:
                report["samples"].append(
                    {
                        "peak": peak,
                        "anchor": norm.get("anchor", "?"),
                        "dur": round(dur, 1),
                        "clip_score": hm.get("clip_score"),
                    }
                )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit MLBB inbox VOD highlight vs banner gates")
    parser.add_argument(
        "--inbox",
        default=os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"),
    )
    parser.add_argument("--limit", type=int, default=12, help="Max VOD files to scan")
    parser.add_argument("--soften", type=int, default=0, choices=(0, 1, 2), help="Simulate adaptive soften level")
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    args = parser.parse_args()

    inbox = Path(args.inbox)
    if not inbox.is_dir():
        print(f"inbox missing: {inbox}", file=sys.stderr)
        return 1

    vods = sorted(inbox.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)[: args.limit]
    if not vods:
        print(f"no VODs in {inbox}", file=sys.stderr)
        return 1

    rows = [audit_vod(v, soften_level=args.soften) for v in vods]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    total_pass = sum(r["pass_peaks"] for r in rows)
    total_motion = sum(r["motion_ok"] for r in rows)
    total_banner = sum(r["banner_ok"] for r in rows)
    total_reject = sum(r["banner_reject"] for r in rows)
    print(f"Audited {len(rows)} VODs | PASS peaks={total_pass} motion_ok={total_motion} banner_ok={total_banner} banner_reject={total_reject}")
    for row in rows:
        print(
            f"  {row['vod']}: pass={row['pass_peaks']} motion={row['motion_ok']} "
            f"banner={row['banner_ok']} reject={row['banner_reject']} L{row.get('active_level', 0)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
