#!/usr/bin/env python3
"""Check YouTube reachability via yt-dlp (independent of TikTok proxy)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from youtube_download import load_env, ytdlp_cmd


def check() -> tuple[bool, str]:
    env = load_env()
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        "--flat-playlist",
        "--playlist-end",
        "1",
        "--print",
        "id",
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    if result.returncode == 0 and (result.stdout or "").strip():
        return True, "ok"
    err = (result.stderr or result.stdout or "")[:200]
    return False, err or f"rc={result.returncode}"


def main() -> int:
    ok, reason = check()
    print(f"youtube_ok={ok} reason={reason}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
