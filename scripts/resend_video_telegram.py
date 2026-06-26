#!/usr/bin/env python3
"""Resend a rendered montage to a Telegram chat (bypasses HTTP_PROXY)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--caption", default="")
    parser.add_argument("--env", type=Path, default=Path("/root/.video_bot.env"))
    args = parser.parse_args()

    env = load_env(args.env)
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = args.chat_id or env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or chat_id missing", file=sys.stderr)
        return 1
    if not args.video.exists():
        print(f"file not found: {args.video}", file=sys.stderr)
        return 1

    caption = args.caption or args.video.stem
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    cmd = [
        "curl",
        "-sS",
        "-m",
        "600",
        "-F",
        f"chat_id={chat_id}",
        "-F",
        "supports_streaming=true",
        "-F",
        f"caption={caption[:900]}",
        "-F",
        f"video=@{args.video}",
        url,
    ]
    clean_env = {k: v for k, v in os.environ.items() if k.lower() not in {
        "http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    }}
    result = subprocess.run(cmd, capture_output=True, text=True, env=clean_env)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(result.stdout or result.stderr, file=sys.stderr)
        return 1
    print(json.dumps({"ok": payload.get("ok"), "description": payload.get("description", "")[:200]}))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
