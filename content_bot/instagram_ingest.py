from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from yt_dlp import YoutubeDL

from .config import InstagramSource


@dataclass(slots=True)
class InstagramPost:
    post_id: str
    source_name: str
    source_url: str
    permalink: str
    caption: str
    media_url: str | None
    thumbnail_url: str | None
    view_count: int | None
    like_count: int | None
    uploader: str | None


def _entry_to_post(source: InstagramSource, entry: dict) -> InstagramPost:
    post_id = str(entry.get("id") or entry.get("display_id") or entry.get("webpage_url"))
    permalink = str(entry.get("webpage_url") or source.url)
    caption = str(entry.get("description") or entry.get("title") or "").strip()
    media_url = entry.get("url")
    thumbnail_url = entry.get("thumbnail")
    return InstagramPost(
        post_id=post_id,
        source_name=source.name,
        source_url=source.url,
        permalink=permalink,
        caption=caption,
        media_url=media_url if isinstance(media_url, str) else None,
        thumbnail_url=thumbnail_url if isinstance(thumbnail_url, str) else None,
        view_count=entry.get("view_count"),
        like_count=entry.get("like_count"),
        uploader=entry.get("uploader") or entry.get("channel"),
    )


def fetch_source_posts(source: InstagramSource) -> list[InstagramPost]:
    return fetch_source_posts_with_options(source)


def fetch_source_posts_with_options(
    source: InstagramSource,
    *,
    cookiefile: str | None = None,
    proxy_url: str | None = None,
) -> list[InstagramPost]:
    options = {
        "extract_flat": False,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "playlistend": source.max_entries,
    }
    if cookiefile:
        options["cookiefile"] = cookiefile
    if proxy_url:
        options["proxy"] = proxy_url

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(source.url, download=False)

    entries: Iterable[dict]
    if info is None:
        return []
    if "entries" in info and isinstance(info["entries"], list):
        entries = [entry for entry in info["entries"] if entry]
    else:
        entries = [info]

    posts = [_entry_to_post(source, entry) for entry in entries]
    # yt-dlp returns newest-first; reverse → oldest first (stable cron order).
    posts.reverse()
    return posts

