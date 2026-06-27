#!/usr/bin/env python3
"""Re-send owner preview for a montage that was built but never previewed in Telegram."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from segment_preview import PROOF_ROOT, build_proof_package, send_proof_to_owner
from strict_segment_gate import GAME_LABELS, normalize_profile
from visual_action_check import verify_segments_visual


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = Path("/root/.video_bot.env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("TG_BOT_TOKEN", "TG_CHAT_ID"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def preview_id_for(game: str, basename: str) -> str:
    slug = f"{normalize_profile(game)}_{basename}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    return slug.replace(" ", "_")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("montage_json", type=Path, help="Path to montage .json sidecar")
    args = parser.parse_args()

    meta = json.loads(args.montage_json.read_text(encoding="utf-8"))
    montage_path = args.montage_json.with_suffix(".mp4")
    if not montage_path.exists():
        print(f"montage mp4 missing: {montage_path}")
        return 1

    env = load_env()
    profile = normalize_profile(meta.get("profile", "pubg"))
    arranged = meta.get("selected_segments") or []
    segment_metrics = meta.get("segment_metrics") or []
    if not arranged:
        print("no selected_segments in json")
        return 1

    vod_path = Path(arranged[0]["source_path"])
    if not vod_path.exists():
        print(f"source vod missing: {vod_path}")
        return 1

    segment_pairs = [
        (
            float(item["start"]),
            float(item.get("input_duration") or item.get("output_duration") or 9.0),
        )
        for item in arranged
    ]
    vis_passed, vis_total, visual_rows, vis_reason = verify_segments_visual(
        vod_path, profile, segment_pairs, segment_metrics=segment_metrics
    )
    if vis_passed < vis_total:
        print(f"visual gate failed: {vis_reason}")
        return 1

    clips = [
        {
            "start": float(item["start"]),
            "input_duration": float(item.get("input_duration") or item.get("output_duration") or 9.0),
            "output_duration": float(item.get("output_duration") or item.get("input_duration") or 9.0),
        }
        for item in arranged
    ]
    game = GAME_LABELS.get(profile, profile)
    pid = preview_id_for(profile, montage_path.stem)
    caption = f"🎬 {game} | повтор превью\nФайл: {montage_path.name}"
    pkg = build_proof_package(
        video_path=vod_path,
        profile=profile,
        game_label=game,
        segments=clips,
        visual_rows=visual_rows,
        audio_metrics=segment_metrics,
        montage_path=montage_path,
        preview_id=pid,
    )
    proof_dir = PROOF_ROOT / pid
    proof_dir.mkdir(parents=True, exist_ok=True)
    (proof_dir / "montage.json").write_text(
        json.dumps({"montage": str(montage_path), "caption": caption, "profile": profile}, indent=2),
        encoding="utf-8",
    )
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("no telegram env")
        return 1
    send_proof_to_owner(pkg, env, skip_rescore=True)
    print(f"OK preview_id={pid} visual={vis_passed}/{vis_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
