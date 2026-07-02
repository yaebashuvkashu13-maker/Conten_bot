#!/usr/bin/env python3
"""Batch-scan long PUBG inbox VODs (no download). Usage: shooter_vod_batch_scan.py [--last N] [--send]"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("SHOOTER_VOD_FEED", "1")
os.environ.setdefault("SHOOTER_VOD_COMBAT_FAST", "1")
os.environ.setdefault("SHOOTER_VOD_COMBAT_ONLY", "1")
os.environ.setdefault("VIDEO_FRAME_IO_FORCE_FFMPEG", "1")

from highlight_scorer import discover_highlight_candidates, normalize_profile  # noqa: E402
from mlbb_vod_segment_feed import _ffprobe_duration, _vod_min_sec  # noqa: E402


def _long_inbox(inbox: Path, limit: int) -> list[Path]:
    rows: list[tuple[float, Path]] = []
    for mp4 in inbox.glob("yt_*.mp4"):
        dur = _ffprobe_duration(mp4)
        if dur >= _vod_min_sec():
            rows.append((mp4.stat().st_mtime, mp4))
    rows.sort(reverse=True)
    return [p for _, p in rows[:limit]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=10)
    ap.add_argument("--game", default="pubg")
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args()
    inbox = Path(os.environ.get("PUBG_VOD_INBOX", "/root/data/pubg/youtube_nightly/inbox"))
    profile = normalize_profile(args.game)
    vods = _long_inbox(inbox, args.last)
    print(f"batch scan game={args.game} long_vods={len(vods)} inbox={inbox}")
    for vod in vods:
        vid = vod.stem[3:] if vod.stem.startswith("yt_") else vod.stem
        dur = _ffprobe_duration(vod)
        print(f"\n=== {vid} dur={dur:.0f}s size_mb={vod.stat().st_size/1e6:.1f} ===")
        try:
            pool = discover_highlight_candidates(vod, profile, limit=5)
        except Exception as exc:
            print(f"FAIL {vid}: {exc}")
            continue
        print(f"pool={len(pool)}")
        for c in pool[:3]:
            hm = c.get("highlight_metrics") or {}
            print(
                f"  start={c.get('start')} panns={hm.get('panns_gun_max')} "
                f"reason={hm.get('pass_reason') or c.get('gate_reason')}"
            )
        if args.send and pool:
            subprocess.run(
                [sys.executable, "-u", str(Path(__file__).parent / "shooter_vod_segment_feed.py"), args.game],
                check=False,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
