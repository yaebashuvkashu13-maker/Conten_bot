#!/usr/bin/env python3
"""Build /root/config.instagram-mlbb.yaml from .video_bot.env (no secrets in git)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

ENV_FILE = Path(os.environ.get("ENV_FILE", "/root/.video_bot.env"))
OUT = Path(os.environ.get("IG_CONFIG_OUT", "/root/config.instagram-mlbb.yaml"))
TEMPLATE = Path(os.environ.get("IG_CONFIG_TEMPLATE", "/root/content_bot_ml/config.instagram-mlbb.yaml"))
if not TEMPLATE.exists():
    TEMPLATE = Path(__file__).resolve().parent.parent / "config.instagram-mlbb.yaml"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def main() -> int:
    env = load_env(ENV_FILE)
    token = env.get("TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN", "")
    chat = env.get("TG_CHAT_ID") or os.environ.get("TG_CHAT_ID", "")
    if not token or not chat:
        print("TG_BOT_TOKEN and TG_CHAT_ID required", file=sys.stderr)
        return 1

    raw = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8")) or {}
    raw["telegram"] = {"bot_token": token, "channel_id": chat}
    raw["dry_run"] = os.environ.get("IG_DIGEST_DRY_RUN", "0") == "1"
    raw["state_path"] = "/root/data/mlbb/instagram_digest_state.json"
    cookies = os.environ.get("INSTAGRAM_COOKIES_PATH", "/root/instagram_cookies.txt")
    if Path(cookies).exists():
        raw["instagram_cookies_path"] = cookies
    else:
        raw["instagram_cookies_path"] = None
    # Do not reuse TikTok LTE proxy by default — IG often breaks on datacenter IPs.
    proxy = os.environ.get("INSTAGRAM_PROXY_URL") or env.get("INSTAGRAM_PROXY_URL") or None
    raw["proxy_url"] = proxy

    max_posts = int(os.environ.get("IG_DIGEST_MAX_POSTS", "7"))
    raw["max_posts_per_run"] = max_posts
    per_source = int(os.environ.get("IG_DIGEST_MAX_PER_SOURCE", "2"))
    for source in raw.get("instagram_sources") or []:
        source["max_entries"] = min(int(source.get("max_entries", 5)), per_source)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"wrote {OUT} bloggers={len(raw.get('instagram_sources', []))} dry_run={raw['dry_run']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
