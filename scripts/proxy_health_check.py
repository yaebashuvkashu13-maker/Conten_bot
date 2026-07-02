#!/usr/bin/env python3
"""Verify TikTok proxy before burning rental time on yt-dlp."""

from __future__ import annotations

import argparse
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


def check_proxy(proxy: str, timeout: int = 25) -> tuple[bool, str]:
    if not proxy:
        return False, "proxy_missing"
    cmd = [
        "yt-dlp",
        "--proxy",
        proxy,
        "--no-warnings",
        "--flat-playlist",
        "--playlist-end",
        "1",
        "--print",
        "id",
        "https://www.tiktok.com/@mlbbttofficial",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    if result.returncode == 0 and result.stdout.strip():
        return True, "ok"
    err = (result.stderr or result.stdout or "").lower()
    if "failed to connect" in err or "connection refused" in err or "could not connect" in err:
        return False, "proxy_unreachable"
    if "ip blocked" in err or "captcha" in err:
        return False, "tiktok_blocked"
    return False, f"yt_dlp_rc={result.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check if TikTok proxy is usable")
    parser.add_argument("--env", type=Path, default=Path("/root/.video_bot.env"))
    parser.add_argument("--proxy", default="", help="Override proxy URL")
    args = parser.parse_args()

    env = load_env(args.env)
    proxy = args.proxy or env.get("YTDLP_PROXY") or env.get("SOCKS5_PROXY") or env.get("PROXY_URL") or ""
    ok, reason = check_proxy(proxy)
    host = ""
    if "@" in proxy:
        host = proxy.split("@")[-1].split("/")[0]
    print(f"proxy_ok={ok} reason={reason} host={host}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
