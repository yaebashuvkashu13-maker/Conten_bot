#!/usr/bin/env python3
"""
Exchange VK OAuth code for community/user token ON THIS SERVER (same IP as cron uploads).

Setup in dev.vk.com:
  - Standalone app, redirect_uri=https://YOUR_HOST/vk/oauth/callback
  - Scopes: video,groups,offline,wall

Usage on VPS:
  python3 vk_mlbb_oauth_token.py --print-url
  python3 vk_mlbb_oauth_token.py --code AUTH_CODE_FROM_REDIRECT
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ENV_FILE = Path("/root/.video_bot.env")
DEFAULT_API_VERSION = "5.199"


def load_env_file() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    for key in (
        "VK_MLBB_APP_ID",
        "VK_APP_ID",
        "VK_MLBB_APP_SECRET",
        "VK_APP_SECRET",
        "VK_MLBB_REDIRECT_URI",
        "VK_OAUTH_REDIRECT_URI",
        "VK_MLBB_GROUP_ID",
    ):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def app_id(env: dict[str, str]) -> str:
    aid = env.get("VK_MLBB_APP_ID") or env.get("VK_APP_ID") or ""
    if not aid:
        raise SystemExit("Set VK_MLBB_APP_ID (or VK_APP_ID) in /root/.video_bot.env")
    return aid


def app_secret(env: dict[str, str]) -> str:
    sec = env.get("VK_MLBB_APP_SECRET") or env.get("VK_APP_SECRET") or ""
    if not sec:
        raise SystemExit("Set VK_MLBB_APP_SECRET in /root/.video_bot.env")
    return sec


def redirect_uri(env: dict[str, str]) -> str:
    uri = env.get("VK_MLBB_REDIRECT_URI") or env.get("VK_OAUTH_REDIRECT_URI") or ""
    if not uri:
        raise SystemExit("Set VK_MLBB_REDIRECT_URI (HTTPS callback on this VPS)")
    return uri


def scopes() -> str:
    return os.environ.get("VK_MLBB_OAUTH_SCOPES", "video,groups,offline,wall")


def print_auth_url(env: dict[str, str]) -> None:
    params = {
        "client_id": app_id(env),
        "display": "page",
        "redirect_uri": redirect_uri(env),
        "scope": scopes(),
        "response_type": "code",
        "v": DEFAULT_API_VERSION,
        "state": "mlbb_upload",
    }
    url = "https://oauth.vk.com/authorize?" + urllib.parse.urlencode(params)
    print("Open in browser (login VK, allow access):")
    print(url)
    print("\nAfter redirect, copy code=... from URL and run:")
    print("  python3 vk_mlbb_oauth_token.py --code YOUR_CODE")


def exchange_code(env: dict[str, str], code: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "client_id": app_id(env),
            "client_secret": app_secret(env),
            "redirect_uri": redirect_uri(env),
            "code": code.strip(),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth.vk.com/access_token",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def save_token(token: str) -> None:
    lines: list[str] = []
    if ENV_FILE.exists():
        lines = [
            ln
            for ln in ENV_FILE.read_text(encoding="utf-8").splitlines()
            if not ln.strip().startswith("VK_MLBB_ACCESS_TOKEN=")
        ]
    lines.append(f"VK_MLBB_ACCESS_TOKEN={token}")
    ENV_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(ENV_FILE, 0o600)
    print(f"OK saved VK_MLBB_ACCESS_TOKEN to {ENV_FILE}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print-url", action="store_true")
    parser.add_argument("--code", default="")
    args = parser.parse_args()
    env = load_env_file()

    if args.print_url:
        print_auth_url(env)
        return 0
    if not args.code:
        print("Use --print-url or --code AUTH_CODE")
        return 1

    body = exchange_code(env, args.code)
    if "error" in body:
        print(f"FAIL oauth: {body}")
        return 1
    token = body.get("access_token", "")
    if not token:
        print(f"FAIL no access_token in {body}")
        return 1
    save_token(token)
    print("Run: python3 vk_mlbb_token_check.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
