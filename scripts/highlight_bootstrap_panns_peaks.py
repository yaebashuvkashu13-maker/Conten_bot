#!/usr/bin/env python3
"""Auto-create good exemplars from top PANNs gun windows on a VOD."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlight_scorer import WINDOW_SEC, score_panns_audio

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
INBOX = Path("/root/data/mlbb/youtube_nightly/inbox")
OUT = Path(os.environ.get("HIGHLIGHT_EXEMPLAR_ROOT", str(REPO / "data" / "highlight_exemplars")))


def cut(vod: Path, start: float, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", "4",
        "-i", str(vod), "-c", "copy", str(dest),
    ]
    return subprocess.run(cmd, capture_output=True, timeout=120).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", default="pubg")
    parser.add_argument("--vod", required=True)
    parser.add_argument("--from-sec", type=int, default=60)
    parser.add_argument("--to-sec", type=int, default=1800)
    parser.add_argument("--step", type=int, default=30)
    parser.add_argument("--min-panns", type=float, default=0.35)
    parser.add_argument("--top", type=int, default=12)
    args = parser.parse_args()

    vod = Path(args.vod)
    if not vod.exists():
        vod = INBOX / args.vod
    if not vod.exists():
        print(f"REFUSED: bootstrap_panns, reason=vod_missing")
        return 1

    hits: list[tuple[float, float]] = []
    for t in range(args.from_sec, args.to_sec, args.step):
        p = score_panns_audio(vod, float(t), WINDOW_SEC)
        if p["panns_gun_max"] >= args.min_panns:
            hits.append((p["panns_gun_max"], float(t)))
    hits.sort(reverse=True)
    good_dir = OUT / args.game / "good"
    n = 0
    for score, t in hits[: args.top]:
        dest = good_dir / f"panns_{int(t)}_{score:.2f}.mp4"
        if cut(vod, t, dest):
            n += 1
    print(f"OK bootstrap_panns game={args.game} clips={n} dir={good_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
