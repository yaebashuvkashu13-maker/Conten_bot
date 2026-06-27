#!/usr/bin/env python3
"""Probe VOD with highlight_scorer — outputs JSON for owner review."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlight_scorer import discover_highlight_candidates, normalize_profile, select_montage_segments

INBOX = Path("/root/data/mlbb/youtube_nightly/inbox")


def segment_key(sig: str, start: float) -> str:
    return f"{sig}:{round(start, 3)}"


def file_sha256(path: Path) -> str:
    import hashlib

    d = hashlib.sha256()
    with path.open("rb") as h:
        for chunk in iter(lambda: h.read(1024 * 1024), b""):
            d.update(chunk)
    return d.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, choices=["pubg", "standoff", "mobile_legends", "genshin", "wot"])
    parser.add_argument("--vod", required=True)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--owner-only", action="store_true", help="Score only owner-labeled good windows (fast)")
    args = parser.parse_args()

    vod = Path(args.vod)
    if not vod.exists():
        vod = INBOX / args.vod
    if not vod.exists():
        print(json.dumps({"status": "REFUSED", "reason": "vod_missing", "visual_passed": "0/0"}))
        return 1

    profile = normalize_profile(args.profile)
    sig = file_sha256(vod)

    if args.owner_only:
        from highlight_scorer import WINDOW_SEC, score_candidate_window, _owner_anchor_starts

        pool = []
        for start in _owner_anchor_starts(vod, profile):
            m = score_candidate_window(vod, max(0, start - 2), WINDOW_SEC, profile)
            pool.append(
                {
                    "start": m.start,
                    "highlight_metrics": m.to_dict(),
                    "score": m.combined_score,
                    "output_duration": WINDOW_SEC,
                }
            )
    else:
        pool = discover_highlight_candidates(vod, profile, sig=sig, segment_key_fn=segment_key, limit=args.limit)
    chosen = select_montage_segments(pool, set(), sig, segment_key)

    rows = [c.get("highlight_metrics") or c.get("strict_metrics") for c in pool[: args.limit]]
    game = profile.upper() if profile != "mobile_legends" else "MLBB"

    if len(chosen) < 3:
        print(
            json.dumps(
                {
                    "status": "REFUSED",
                    "game": game,
                    "reason": f"segments={len(chosen)}/3",
                    "visual_passed": f"{len(pool)}/{args.limit}",
                    "candidates": rows,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "status": "CANDIDATES_OK",
                "game": game,
                "segments": len(chosen),
                "visual_passed": f"{len(pool)}/{args.limit}",
                "montage": [c.get("highlight_metrics") for c in chosen],
                "all_candidates": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
