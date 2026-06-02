from __future__ import annotations

import argparse
import logging
import time

from .config import load_config
from .instagram_ingest import fetch_source_posts_with_options
from .state import StateStore
from .telegram_publisher import TelegramPublisher

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")


def run(config_path: str) -> int:
    config = load_config(config_path)
    state = StateStore(config.state_path)
    publisher = TelegramPublisher(config.telegram, dry_run=config.dry_run)

    published = 0
    errors = 0
    for source in config.instagram_sources:
        if published >= config.max_posts_per_run:
            break
        try:
            posts = fetch_source_posts_with_options(
                source,
                cookiefile=str(config.instagram_cookies_path) if config.instagram_cookies_path else None,
                proxy_url=config.proxy_url,
            )
        except Exception as exc:
            logging.warning("fetch failed source=%s: %s", source.name, exc)
            errors += 1
            continue
        for post in posts:
            if published >= config.max_posts_per_run:
                break
            if post.post_id in state.published_ids:
                continue
            try:
                publisher.publish_post(post)
                state.mark_published(post.post_id)
                published += 1
                time.sleep(1.2)
            except Exception as exc:
                logging.warning("publish failed source=%s post=%s: %s", source.name, post.post_id, exc)
                errors += 1

    logging.info("digest finished published=%s errors=%s", published, errors)
    print(f"Published {published} new posts.")
    return 0 if published > 0 or errors == 0 else 1


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

