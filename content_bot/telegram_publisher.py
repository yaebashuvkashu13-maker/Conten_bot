from __future__ import annotations

import json
import urllib.parse
import urllib.request

from .config import TelegramConfig
from .instagram_ingest import InstagramPost


def build_caption(post: InstagramPost) -> str:
    stats = []
    if post.view_count:
        stats.append(f"views: {post.view_count}")
    if post.like_count:
        stats.append(f"likes: {post.like_count}")

    body = post.caption.strip() or "Новый пост из Instagram"
    lines = [
        f"📌 {post.source_name}",
        body,
        "",
        f"Source: {post.permalink}",
    ]
    if stats:
        lines.append(" | ".join(stats))
    return "\n".join(lines)[:1024]


class TelegramPublisher:
    def __init__(self, config: TelegramConfig, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run

    def _request(self, method: str, payload: dict) -> dict:
        if self.dry_run:
            return {"ok": True, "result": {"dry_run": True, "method": method, "payload": payload}}

        encoded = urllib.parse.urlencode(payload).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.config.bot_token}/{method}",
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode())
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")
        return result

    def publish_post(self, post: InstagramPost) -> dict:
        caption = build_caption(post)
        if post.thumbnail_url:
            return self._request(
                "sendPhoto",
                {
                    "chat_id": self.config.channel_id,
                    "photo": post.thumbnail_url,
                    "caption": caption,
                },
            )
        return self._request(
            "sendMessage",
            {
                "chat_id": self.config.channel_id,
                "text": caption,
                "disable_web_page_preview": False,
            },
        )

