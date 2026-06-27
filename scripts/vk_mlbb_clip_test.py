#!/usr/bin/env python3
"""Test-upload one MLBB clip to VK community via shortVideo.create."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vk_mlbb_upload import load_env, publish_short_clip, token_permissions_summary, vk_token


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload one vertical clip to VK (shortVideo API)")
    parser.add_argument("video", type=Path, help="Source mp4 path")
    parser.add_argument("--title", default="")
    parser.add_argument("--description", default="Mobile Legends")
    parser.add_argument("--publish", action="store_true", help="Call shortVideo.publish after upload")
    args = parser.parse_args()

    if not args.video.exists():
        print(f"FAIL missing file: {args.video}", file=sys.stderr)
        return 1

    env = load_env()
    token = vk_token(env)
    print(f"token_permissions={token_permissions_summary(token, env)}")

    try:
        result = publish_short_clip(
            args.video,
            title=args.title,
            description=args.description,
            publish=args.publish,
            env=env,
        )
    except RuntimeError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1

    owner = result.get("owner_id")
    vid = result.get("video_id")
    print(f"OK clip uploaded owner_id={owner} video_id={vid}")
    if owner and vid:
        print(f"link=https://vk.com/video{owner}_{vid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
