from __future__ import annotations

import argparse
import time

from .config import load_config
from .instagram_ingest import fetch_source_posts_with_options
from .state import StateStore
from .telegram_publisher import TelegramPublisher


def run(config_path: str) -> int:
    config = load_config(config_path)
    state = StateStore(config.state_path)
    publisher = TelegramPublisher(
        config.telegram,
        dry_run=config.dry_run,
        max_retries=config.request_max_retries,
    )

    published = 0
    skipped = 0
    failed_sources = 0
    for source in config.instagram_sources:
        try:
            posts = fetch_source_posts_with_options(
                source,
                cookiefile=str(config.instagram_cookies_path) if config.instagram_cookies_path else None,
                proxy_url=config.proxy_url,
            )
        except Exception as exc:
            failed_sources += 1
            print(f"[{source.name}] fetch failed: {exc}")
            continue

        print(f"[{source.name}] fetched {len(posts)} posts")
        for post in posts:
            if post.post_id in state.published_ids:
                skipped += 1
                continue
            try:
                publisher.publish_post(post)
            except Exception as exc:
                print(f"[{source.name}] publish failed for {post.post_id}: {exc}")
                continue
            state.mark_published(post.post_id)
            published += 1
            if config.publish_delay_seconds > 0:
                time.sleep(config.publish_delay_seconds)

    print(
        f"Done. published={published}, skipped={skipped}, failed_sources={failed_sources}, "
        f"dry_run={config.dry_run}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish Instagram posts into Telegram.")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config file.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return run(args.config)


if __name__ == "__main__":
    raise SystemExit(main())

