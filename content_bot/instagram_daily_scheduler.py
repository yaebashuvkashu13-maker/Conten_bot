from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .instagram_reels_pipeline import run_once


def _next_run_at(hour: int, minute: int, timezone: ZoneInfo) -> datetime:
    now = datetime.now(timezone)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def run_scheduler(
    *,
    hour: int,
    minute: int,
    timezone_name: str,
    config_path: str,
    cookies_path: str,
    proxy_url: str | None,
    state_path: str,
    profile_cache_dir: str,
    max_posts: int,
    page_size: int,
    once: bool,
) -> None:
    timezone = ZoneInfo(timezone_name)
    while True:
        run_at = _next_run_at(hour, minute, timezone)
        sleep_seconds = max((run_at - datetime.now(timezone)).total_seconds(), 0)
        print(f"Next Instagram run at {run_at.isoformat()} ({timezone_name})", flush=True)
        time.sleep(sleep_seconds)

        try:
            sent = run_once(
                config_path=config_path,
                cookies_path=cookies_path,
                proxy_url=proxy_url,
                state_path=state_path,
                bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
                chat_id=os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHANNEL_ID"),
                page_size=page_size,
                max_posts=max_posts,
                profile_cache_dir=profile_cache_dir,
                dry_run=False,
            )
            print(f"Instagram daily run sent {sent} reels.", flush=True)
        except Exception as exc:
            print(f"Instagram daily run failed: {exc}", flush=True)

        if once:
            return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Instagram Reels -> Telegram daily at a fixed local time.")
    parser.add_argument("--time", default="18:00", help="Local run time in HH:MM, default 18:00.")
    parser.add_argument("--timezone", default="Europe/Moscow")
    parser.add_argument("--config", default="config.instagram-mlbb.yaml")
    parser.add_argument("--cookies-path", default=os.environ.get("INSTAGRAM_COOKIES_PATH", "instagram_cookies.cookies"))
    parser.add_argument("--proxy-url", default=os.environ.get("INSTAGRAM_PROXY_URL") or os.environ.get("PROXY_URL"))
    parser.add_argument("--state-path", default="datasets/instagram/reels_state.json")
    parser.add_argument("--profile-cache-dir", default="datasets/instagram")
    parser.add_argument("--max-posts", type=int, default=3)
    parser.add_argument("--page-size", type=int, default=12)
    parser.add_argument("--once", action="store_true", help="Run the next scheduled execution only, then exit.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    hour_raw, minute_raw = args.time.split(":", 1)
    run_scheduler(
        hour=int(hour_raw),
        minute=int(minute_raw),
        timezone_name=args.timezone,
        config_path=args.config,
        cookies_path=args.cookies_path,
        proxy_url=args.proxy_url,
        state_path=args.state_path,
        profile_cache_dir=args.profile_cache_dir,
        max_posts=args.max_posts,
        page_size=args.page_size,
        once=args.once,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
