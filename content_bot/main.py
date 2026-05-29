from __future__ import annotations

import argparse

from .config import load_config
from .instagram_ingest import fetch_source_posts
from .state import StateStore
from .telegram_publisher import TelegramPublisher


def run(config_path: str) -> int:
    config = load_config(config_path)
    state = StateStore(config.state_path)
    publisher = TelegramPublisher(config.telegram, dry_run=config.dry_run)

    published = 0
    for source in config.instagram_sources:
        posts = fetch_source_posts(source)
        for post in posts:
            if post.post_id in state.published_ids:
                continue
            publisher.publish_post(post)
            state.mark_published(post.post_id)
            published += 1

    print(f"Published {published} new posts.")
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

