from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import TelegramConfig
from .instagram_ingest import InstagramPost

logger = logging.getLogger(__name__)


def build_caption(post: InstagramPost) -> str:
    stats = []
    if post.view_count is not None:
        stats.append(f"views: {post.view_count}")
    if post.like_count is not None:
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
        self.max_retries = 4

    def _request(self, method: str, payload: dict) -> dict:
        if self.dry_run:
            return {"ok": True, "result": {"dry_run": True, "method": method, "payload": payload}}

        encoded = urllib.parse.urlencode(payload).encode()
        url = f"https://api.telegram.org/bot{self.config.bot_token}/{method}"
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            request = urllib.request.Request(
                url,
                data=encoded,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    result = json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                body = exc.read().decode(errors="replace") if exc.fp else ""
                retry_after = 0.0
                if exc.code == 429:
                    retry_after = float(exc.headers.get("Retry-After", "3") or 3)
                last_exc = RuntimeError(f"Telegram HTTP {exc.code}: {body[:300]}")
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    delay = retry_after or (2**attempt * 0.5)
                    logger.warning("telegram %s retry %s/%s in %.1fs", method, attempt, self.max_retries, delay)
                    time.sleep(delay)
                    continue
                raise last_exc from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    delay = 2**attempt * 0.5
                    logger.warning("telegram %s retry %s/%s: %s", method, attempt, self.max_retries, exc)
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"Telegram request failed: {exc}") from exc

            if not result.get("ok"):
                description = str(result.get("description", result))
                if "retry after" in description.lower() and attempt < self.max_retries:
                    time.sleep(3 * attempt)
                    continue
                raise RuntimeError(f"Telegram API error: {result}")
            return result

        raise RuntimeError(f"Telegram request failed after retries: {last_exc}")

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
