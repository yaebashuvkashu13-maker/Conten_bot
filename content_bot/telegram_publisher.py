from __future__ import annotations

import json
import time
import urllib.error
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
    def __init__(
        self,
        config: TelegramConfig,
        *,
        dry_run: bool = False,
        max_retries: int = 5,
    ) -> None:
        self.config = config
        self.dry_run = dry_run
        self.max_retries = max(1, max_retries)

    def _retry_after_seconds(self, attempt: int, result: dict | None = None) -> float:
        if result:
            parameters = result.get("parameters") or {}
            retry_after = parameters.get("retry_after")
            if isinstance(retry_after, (int, float)) and retry_after > 0:
                return float(retry_after)
        return min(60.0, 2.0**attempt)

    def _request_once(self, method: str, payload: dict) -> dict:
        encoded = urllib.parse.urlencode(payload).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.config.bot_token}/{method}",
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode())

    def _request(self, method: str, payload: dict) -> dict:
        if self.dry_run:
            return {"ok": True, "result": {"dry_run": True, "method": method, "payload": payload}}

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                result = self._request_once(method, payload)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt + 1 < self.max_retries:
                    retry_after = exc.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else self._retry_after_seconds(attempt)
                    except (TypeError, ValueError):
                        delay = self._retry_after_seconds(attempt)
                    print(f"Telegram HTTP 429, retry in {delay:.1f}s ({attempt + 1}/{self.max_retries})")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"Telegram HTTP error {exc.code}: {exc.reason}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt + 1 >= self.max_retries:
                    break
                delay = self._retry_after_seconds(attempt)
                print(f"Telegram network error, retry in {delay:.1f}s ({attempt + 1}/{self.max_retries})")
                time.sleep(delay)
                continue

            if result.get("ok"):
                return result

            error_code = result.get("error_code")
            if error_code == 429 and attempt + 1 < self.max_retries:
                delay = self._retry_after_seconds(attempt, result)
                print(f"Telegram flood control, retry in {delay:.1f}s ({attempt + 1}/{self.max_retries})")
                time.sleep(delay)
                continue

            raise RuntimeError(f"Telegram API error: {result}")

        if last_error is not None:
            raise RuntimeError(f"Telegram request failed after {self.max_retries} attempts") from last_error
        raise RuntimeError(f"Telegram request failed after {self.max_retries} attempts")

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
