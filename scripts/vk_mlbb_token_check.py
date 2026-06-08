#!/usr/bin/env python3
"""Verify VK_MLBB_ACCESS_TOKEN works from THIS host IP (avoids error 5)."""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vk_mlbb_upload import load_env, vk_call, vk_group_id, vk_token


def host_ip_hint() -> str:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=15) as resp:
            return resp.read().decode().strip()
    except Exception:
        return "unknown"


def main() -> int:
    env = load_env()
    token = vk_token(env)
    group_id = vk_group_id(env)
    ip = host_ip_hint()
    print(f"host_outbound_ip={ip}")
    print(f"group_id={group_id}")

    try:
        users = vk_call("users.get", {}, token)
        name = users[0].get("first_name", "") if users else "?"
        print(f"OK users.get: {name}")
    except RuntimeError as exc:
        msg = str(exc)
        print(f"FAIL users.get: {msg}")
        if "error 5" in msg or "another ip" in msg.lower():
            print(
                "HINT: token issued from another IP — see docs/VK_MLBB_TOKEN.md "
                "(re-issue on VPS via vk_mlbb_oauth_token.py or disable IP bind in VK app)"
            )
        return 1

    try:
        save = vk_call(
            "video.save",
            {
                "name": "token_check",
                "description": "connectivity test — do not publish",
                "group_id": group_id,
                "wallpost": 0,
                "is_private": 1,
            },
            token,
        )
        upload_url = save.get("upload_url", "")
        vid = save.get("video_id", "")
        print(f"OK video.save: video_id={vid} upload_url={'yes' if upload_url else 'no'}")
        print("OK token_valid_from_this_host")
        return 0
    except RuntimeError as exc:
        msg = str(exc)
        print(f"FAIL video.save: {msg}")
        if "error 5" in msg or "another ip" in msg.lower():
            print("HINT: docs/VK_MLBB_TOKEN.md")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
