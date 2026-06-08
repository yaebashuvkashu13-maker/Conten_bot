#!/usr/bin/env python3
"""Score owner-labeled windows only — fast path for montage."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlight_scorer import WINDOW_SEC, _owner_anchor_starts, normalize_profile, score_candidate_window


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--vod", required=True)
    args = parser.parse_args()

    vod = Path(args.vod)
    if not vod.exists():
        vod = Path("/root/data/mlbb/youtube_nightly/inbox") / args.vod
    if not vod.exists():
        print(f"REFUSED vod_missing {vod}")
        return 1

    profile = normalize_profile(args.profile)
    anchors = _owner_anchor_starts(vod, profile)
    if not anchors:
        print(f"REFUSED no_owner_anchors profile={profile}")
        return 1

    passed = 0
    for t in anchors:
        m = score_candidate_window(vod, max(0.0, t - 2.0), WINDOW_SEC, profile)
        ok = m.rule_pass and m.visual_pass
        if ok:
            passed += 1
        print(
            f"start={int(t)} pass={ok} rule={m.rule_pass} visual={m.visual_pass} "
            f"reason={m.pass_reason} clip={m.clip_score:.3f} "
            f"mini={m.minimap_delta:.4f} skill={m.skill_delta:.4f}"
        )
    print(f"SUMMARY passed={passed}/{len(anchors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
