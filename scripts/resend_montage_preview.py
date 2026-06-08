#!/usr/bin/env python3
"""Re-send owner preview for a montage that was built but never previewed in Telegram."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from smart_video_editor import maybe_send_owner_preview


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
    profile = meta.get("profile", "pubg")
    arranged = meta.get("selected_segments") or []
    segment_metrics = meta.get("segment_metrics") or []
    caption = (
        f"🎬 {profile.upper()} | повтор превью\n"
        f"Файл: {montage_path.name}"
    )
    pid = maybe_send_owner_preview(
        output_path=montage_path,
        arranged=arranged,
        profile=profile,
        bot_token=env.get("TG_BOT_TOKEN", ""),
        chat_id=env.get("TG_CHAT_ID", ""),
        caption=caption,
        segment_metrics=segment_metrics,
    )
    if not pid:
        print("preview refused or skipped")
        return 1
    print(f"OK preview_id={pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
