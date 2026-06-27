#!/usr/bin/env python3
"""Build montage from owner good labels — fast path when full VOD scan is too slow."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlight_scorer import WINDOW_SEC, _owner_anchor_starts, normalize_profile, score_candidate_window
from strict_montage_direct import (
    MIN_CLIPS,
    MIN_GAP_SEC,
    apply_strict_env,
    build_and_send,
    file_sha256,
    segment_key,
)


def _owner_labels_path(profile: str) -> Path:
    from highlight_scorer import _owner_labels_path

    path = _owner_labels_path(profile)
    if path is None or not path.exists():
        raise FileNotFoundError(f"no owner labels for {profile}")
    return path


def owner_good_starts(vod: Path, profile: str) -> list[float]:
    profile = normalize_profile(profile)
    path = _owner_labels_path(profile)
    data = json.loads(path.read_text(encoding="utf-8"))
    vid = vod.stem[3:] if vod.stem.startswith("yt_") else vod.stem
    return [float(r["time_sec"]) for r in data.get("videos", {}).get(vid, []) if r.get("label") == "good"]


def pick_owner_clips(vod: Path, profile: str, sig: str, *, min_clips: int = MIN_CLIPS) -> list[dict]:
    profile = normalize_profile(profile)
    anchors = sorted(owner_good_starts(vod, profile))
    if not anchors:
        raise RuntimeError(f"no good owner labels for {vod.name}")

    scored: list[tuple[float, dict]] = []
    for anchor in anchors:
        start = max(0.0, anchor - 2.0)
        m = score_candidate_window(vod, start, WINDOW_SEC, profile)
        ok = m.rule_pass and m.visual_pass
        if not ok:
            continue
        scored.append(
            (
                m.combined_score or m.clip_score,
                {
                    "source_path": str(vod),
                    "game_name": profile,
                    "start": round(start, 3),
                    "input_duration": WINDOW_SEC,
                    "output_duration": WINDOW_SEC,
                    "speed": 1.0,
                    "score": m.combined_score or m.clip_score,
                    "strict_score": m.combined_score or m.clip_score,
                    "highlight_metrics": m.to_dict(),
                    "gate_reason": m.pass_reason,
                    "strict_metrics": m.to_dict(),
                    "source_signature": sig,
                    "source_index": 0,
                },
            )
        )

    scored.sort(key=lambda row: row[0], reverse=True)
    chosen: list[dict] = []
    for _score, cand in scored:
        start = float(cand["start"])
        if any(abs(start - float(c["start"])) < MIN_GAP_SEC for c in chosen):
            continue
        chosen.append(cand)
        if len(chosen) >= 4:
            break

    if len(chosen) < min_clips:
        raise RuntimeError(f"owner_pass={len(chosen)}/{min_clips} from {len(anchors)} labels")
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--vod", required=True)
    parser.add_argument("--basename", default="")
    parser.add_argument("--caption", default="")
    args = parser.parse_args()

    vod = Path(args.vod)
    if not vod.exists():
        vod = Path("/root/data/mlbb/youtube_nightly/inbox") / args.vod
    if not vod.exists():
        print(f"REFUSED vod_missing {vod}")
        return 2

    profile = normalize_profile(args.profile)
    env = apply_strict_env(profile, dict(os.environ))
    sig = file_sha256(vod)
    try:
        clips = pick_owner_clips(vod, profile, sig)
    except RuntimeError as exc:
        print(f"REFUSED: game={profile}, reason={exc}, visual_passed=0/0")
        return 1

    basename = args.basename or f"owner_{profile}_{time.strftime('%Y%m%d_%H%M%S')}"
    caption = args.caption or f"{profile.upper()} owner-label montage"
    out_dir = Path(env.get("OUTPUT_DIR", "/root/videos"))
    result = build_and_send(
        vod,
        profile,
        clips,
        output_dir=out_dir,
        basename=basename,
        caption=caption,
        chat_id=env.get("TG_CHAT_ID", ""),
        bot_token=env.get("TG_BOT_TOKEN", ""),
        sig=sig,
    )
    if result is None:
        print(f"REFUSED: game={profile}, reason=preview_gate_failed, visual_passed=0/{len(clips)}")
        return 1
    print(f"OK preview montage={result.name} segments={len(clips)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
