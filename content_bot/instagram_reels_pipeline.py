from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any

import requests

from .config import InstagramSource, load_config


INSTAGRAM_APP_ID = "936619743392459"
DEFAULT_STATE_PATH = Path("datasets/instagram/reels_state.json")
AD_KEYWORDS = (
    "anti losstreak",
    "available",
    "bca",
    "contact admin",
    "dana",
    "diamond via login",
    "dm admin",
    "gopay",
    "gift card",
    "harga transparan",
    "jasa joki",
    "legal & aman",
    "monthly",
    "open gift",
    "open order",
    "order sekarang",
    "ovo",
    "paket border",
    "pembayaran",
    "price list",
    "pricelist",
    "proses cepat",
    "qris",
    "shopeepay",
    "skin impian",
    "stok aman",
    "termurah",
    "top up",
    "topup",
    "via gift",
    "whatsapp",
    "wdp",
)
AD_REGEXES = (
    re.compile(r"\brp\.?\s?\d", re.IGNORECASE),
    re.compile(r"\bwa[:\s+]", re.IGNORECASE),
    re.compile(r"\b08\d{6,}\b"),
)


@dataclass(slots=True)
class InstagramMedia:
    media_id: str
    code: str
    username: str
    source_name: str
    media_kind: str
    caption: str
    permalink: str
    image_urls: list[str]
    video_url: str | None
    thumbnail_url: str | None
    play_count: int | None
    like_count: int | None
    comment_count: int | None


def _username_from_url(url: str) -> str:
    match = re.search(r"instagram\.com/([^/?#]+)/?", url)
    if not match:
        raise ValueError(f"Cannot parse Instagram username from URL: {url}")
    return match.group(1)


def advertising_reason(media: InstagramMedia) -> str | None:
    text = " ".join(
        (
            media.caption,
            media.source_name,
            media.username,
        )
    ).lower()
    for keyword in AD_KEYWORDS:
        if keyword in text:
            return f"keyword:{keyword}"
    for pattern in AD_REGEXES:
        if pattern.search(text):
            return f"pattern:{pattern.pattern}"
    return None


def _load_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(str(value) for value in raw.get("sent_codes", []))


def _save_state(path: Path, sent_codes: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sent_codes": sorted(sent_codes)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _csrf_from_cookiejar(cookiejar: MozillaCookieJar) -> str:
    for cookie in cookiejar:
        if cookie.name == "csrftoken":
            return cookie.value
    return ""


def build_session(cookies_path: str | Path, proxy_url: str | None = None) -> requests.Session:
    cookiejar = MozillaCookieJar(str(cookies_path))
    cookiejar.load(ignore_discard=True, ignore_expires=True)

    session = requests.Session()
    session.cookies.update(cookiejar)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "X-IG-App-ID": INSTAGRAM_APP_ID,
            "X-CSRFToken": _csrf_from_cookiejar(cookiejar),
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session


def _cached_user_id(username: str, profile_cache_dir: Path | None) -> str | None:
    if not profile_cache_dir:
        return None
    profile_path = profile_cache_dir / f"{username}_profile.json"
    if not profile_path.exists():
        return None
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    user_id = data.get("data", {}).get("user", {}).get("id")
    return str(user_id) if user_id else None


def fetch_user_id(
    session: requests.Session,
    username: str,
    *,
    profile_cache_dir: Path | None = None,
) -> str:
    cached = _cached_user_id(username, profile_cache_dir)
    if cached:
        return cached

    response = session.get(
        "https://www.instagram.com/api/v1/users/web_profile_info/",
        params={"username": username},
        headers={"Referer": f"https://www.instagram.com/{username}/"},
        timeout=45,
    )
    response.raise_for_status()
    user = response.json().get("data", {}).get("user") or {}
    user_id = user.get("id")
    if not user_id:
        raise RuntimeError(f"Instagram profile did not return user id for {username}")
    return str(user_id)


def _best_image_url(media: dict) -> str | None:
    candidates = media.get("image_versions2", {}).get("candidates") or []
    if not candidates:
        return None
    return candidates[0].get("url")


def _media_from_item(media: dict, *, username: str, source_name: str) -> InstagramMedia | None:
    code = str(media.get("code") or "")
    if not code:
        return None
    caption = ""
    if media.get("caption"):
        caption = str(media["caption"].get("text") or "")

    media_type = int(media.get("media_type") or 0)
    video_versions = media.get("video_versions") or []
    image_urls: list[str] = []
    media_kind = "post"
    if media_type == 1:
        media_kind = "photo"
        image_url = _best_image_url(media)
        if image_url:
            image_urls.append(image_url)
    elif media_type == 8:
        media_kind = "carousel"
        for child in media.get("carousel_media") or []:
            if int(child.get("media_type") or 0) == 1:
                image_url = _best_image_url(child)
                if image_url:
                    image_urls.append(image_url)
        if not image_urls:
            image_url = _best_image_url(media)
            if image_url:
                image_urls.append(image_url)
    elif media_type == 2:
        media_kind = "video"

    return InstagramMedia(
        media_id=str(media.get("id") or media.get("strong_id__") or code),
        code=code,
        username=username,
        source_name=source_name,
        media_kind=media_kind,
        caption=caption,
        permalink=f"https://www.instagram.com/p/{code}/",
        image_urls=image_urls,
        video_url=video_versions[0].get("url") if video_versions else None,
        thumbnail_url=_best_image_url(media),
        play_count=media.get("play_count") or media.get("view_count"),
        like_count=media.get("like_count"),
        comment_count=media.get("comment_count"),
    )


def fetch_feed_media(
    session: requests.Session,
    source: InstagramSource,
    *,
    page_size: int,
    profile_cache_dir: Path | None = None,
) -> list[InstagramMedia]:
    username = _username_from_url(source.url)
    user_id = fetch_user_id(session, username, profile_cache_dir=profile_cache_dir)
    response = session.get(
        f"https://www.instagram.com/api/v1/feed/user/{user_id}/",
        params={"count": str(page_size)},
        headers={"Referer": f"https://www.instagram.com/{username}/"},
        timeout=60,
    )
    response.raise_for_status()
    items = response.json().get("items") or []
    media_items = [
        parsed
        for item in items
        if (parsed := _media_from_item(item, username=username, source_name=source.name)) is not None
    ]
    # Pictures are the SMM priority: photos/carousels first, then videos.
    priority = {"photo": 0, "carousel": 1, "video": 2}
    media_items.sort(key=lambda item: priority.get(item.media_kind, 9))
    return media_items


def fetch_reels(
    session: requests.Session,
    source: InstagramSource,
    *,
    page_size: int,
    profile_cache_dir: Path | None = None,
) -> list[InstagramMedia]:
    username = _username_from_url(source.url)
    user_id = fetch_user_id(session, username, profile_cache_dir=profile_cache_dir)
    response = session.post(
        "https://www.instagram.com/api/v1/clips/user/",
        data={
            "target_user_id": user_id,
            "page_size": str(page_size),
            "include_feed_video": "true",
        },
        headers={"Referer": f"https://www.instagram.com/{username}/reels/"},
        timeout=60,
    )
    response.raise_for_status()
    items = response.json().get("items") or []

    reels: list[InstagramMedia] = []
    for item in items:
        media = item.get("media") if isinstance(item, dict) else None
        if not media:
            continue
        parsed = _media_from_item(media, username=username, source_name=source.name)
        if parsed:
            parsed.media_kind = "video"
            parsed.permalink = f"https://www.instagram.com/reel/{parsed.code}/"
            reels.append(parsed)
    return reels


def build_ready_caption(media: InstagramMedia) -> str:
    stats: list[str] = []
    if media.play_count is not None:
        stats.append(f"{media.play_count:,} просмотров".replace(",", " "))
    if media.like_count is not None:
        stats.append(f"{media.like_count:,} лайков".replace(",", " "))
    if media.comment_count is not None:
        stats.append(f"{media.comment_count:,} комментариев".replace(",", " "))

    original = media.caption.strip() or "Без подписи."
    kind_text = {
        "photo": "Новый пост-картинка по Mobile Legends.",
        "carousel": "Новая карусель по Mobile Legends.",
        "video": "Новый Reels/видео по Mobile Legends.",
    }.get(media.media_kind, "Новый Instagram-пост по Mobile Legends.")
    lines = [
        f"🎮 MLBB | {media.source_name}",
        "",
        kind_text,
        "",
        "Оригинальный текст:",
        original[:900],
        "",
        "Готовая подача для Telegram:",
        "Посмотрите свежий MLBB-ролик. Что думаете: это полезный инсайд или просто хайп?",
        "",
        f"Источник: {media.permalink}",
    ]
    if stats:
        lines.append("Статистика: " + " | ".join(stats))
    lines.append("")
    lines.append("#MLBB #MobileLegends")
    return "\n".join(lines)[:1024]


def _telegram_request(token: str, method: str, **kwargs: Any) -> dict[str, Any]:
    response = requests.post(f"https://api.telegram.org/bot{token}/{method}", timeout=180, **kwargs)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result


def _download_temp_video(session: requests.Session, url: str) -> Path:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    path = Path(handle.name)
    handle.close()
    with session.get(url, stream=True, timeout=180) as response:
        response.raise_for_status()
        with path.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)
    return path


def send_media_to_telegram(
    session: requests.Session,
    media: InstagramMedia,
    *,
    bot_token: str,
    chat_id: str,
    dry_run: bool,
) -> None:
    caption = build_ready_caption(media)
    if dry_run:
        print(f"[{media.media_kind.upper()}] {media.permalink}")
        print(caption)
        return

    if media.image_urls:
        # Telegram can hang on large remote media groups; one image plus source link is more reliable
        # for a daily review queue, and the caption keeps the original carousel URL.
        _telegram_request(
            bot_token,
            "sendPhoto",
            data={"chat_id": chat_id, "photo": media.image_urls[0], "caption": caption},
        )
        return

    if media.video_url:
        temp_path = _download_temp_video(session, media.video_url)
        try:
            with temp_path.open("rb") as handle:
                _telegram_request(
                    bot_token,
                    "sendVideo",
                    data={"chat_id": chat_id, "caption": caption},
                    files={"video": handle},
                )
        finally:
            temp_path.unlink(missing_ok=True)
        return

    _telegram_request(
        bot_token,
        "sendMessage",
        data={"chat_id": chat_id, "text": caption, "disable_web_page_preview": False},
    )


def run_once(
    *,
    config_path: str | Path,
    cookies_path: str | Path,
    proxy_url: str | None,
    state_path: str | Path,
    bot_token: str | None,
    chat_id: str | None,
    page_size: int,
    max_posts: int,
    profile_cache_dir: str | Path | None,
    skip_ads: bool,
    dry_run: bool,
) -> int:
    config = load_config(config_path)
    session = build_session(cookies_path, proxy_url)
    state_file = Path(state_path)
    sent_codes = _load_state(state_file)

    sent = 0
    for source in config.instagram_sources:
        try:
            feed_media = fetch_feed_media(
                session,
                source,
                page_size=min(page_size, max(source.max_entries, 1)),
                profile_cache_dir=Path(profile_cache_dir) if profile_cache_dir else None,
            )
        except Exception as exc:
            print(f"Skipping Instagram source {source.name}: {exc}")
            continue
        for media in feed_media:
            if media.code in sent_codes:
                continue
            if skip_ads and (reason := advertising_reason(media)):
                print(f"Skipping ad-like Instagram media {media.code}: {reason}")
                continue
            if not dry_run and (not bot_token or not chat_id):
                raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required unless --dry-run is used.")
            send_media_to_telegram(
                session,
                media,
                bot_token=bot_token or "",
                chat_id=chat_id or "",
                dry_run=dry_run,
            )
            if not dry_run:
                sent_codes.add(media.code)
                _save_state(state_file, sent_codes)
            sent += 1
            if sent >= max_posts:
                return sent
            time.sleep(1)

    # Fallback for accounts where regular feed returns no new items but Reels still work.
    for source in config.instagram_sources:
        try:
            reels = fetch_reels(
                session,
                source,
                page_size=min(page_size, max(source.max_entries, 1)),
                profile_cache_dir=Path(profile_cache_dir) if profile_cache_dir else None,
            )
        except Exception as exc:
            print(f"Skipping Instagram Reels fallback {source.name}: {exc}")
            continue
        for media in reels:
            if media.code in sent_codes:
                continue
            if skip_ads and (reason := advertising_reason(media)):
                print(f"Skipping ad-like Instagram media {media.code}: {reason}")
                continue
            if not dry_run and (not bot_token or not chat_id):
                raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required unless --dry-run is used.")
            send_media_to_telegram(
                session,
                media,
                bot_token=bot_token or "",
                chat_id=chat_id or "",
                dry_run=dry_run,
            )
            if not dry_run:
                sent_codes.add(media.code)
                _save_state(state_file, sent_codes)
            sent += 1
            if sent >= max_posts:
                return sent
            time.sleep(1)
    return sent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Instagram posts/Reels via cookies and send them to Telegram.")
    parser.add_argument("--config", default="config.instagram-mlbb.yaml")
    parser.add_argument("--cookies-path", default=os.environ.get("INSTAGRAM_COOKIES_PATH", "instagram_cookies.cookies"))
    parser.add_argument("--proxy-url", default=os.environ.get("INSTAGRAM_PROXY_URL") or os.environ.get("PROXY_URL"))
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--profile-cache-dir", default="datasets/instagram")
    parser.add_argument("--telegram-token-env", default="TELEGRAM_BOT_TOKEN")
    parser.add_argument("--telegram-chat-id-env", default="TELEGRAM_CHAT_ID")
    parser.add_argument("--page-size", type=int, default=12)
    parser.add_argument("--max-posts", type=int, default=3)
    parser.add_argument("--include-ads", action="store_true", help="Allow ad-like posts; by default they are skipped.")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    sent = run_once(
        config_path=args.config,
        cookies_path=args.cookies_path,
        proxy_url=args.proxy_url,
        state_path=args.state_path,
        bot_token=os.environ.get(args.telegram_token_env),
        chat_id=os.environ.get(args.telegram_chat_id_env) or os.environ.get("TELEGRAM_CHANNEL_ID"),
        page_size=args.page_size,
        max_posts=args.max_posts,
        profile_cache_dir=args.profile_cache_dir,
        skip_ads=not args.include_ads,
        dry_run=args.dry_run,
    )
    print(f"Sent {sent} Instagram media posts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
