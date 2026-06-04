#!/usr/bin/env python3
"""Instagram blogger digest via gallery-dl + cookies (works when yt-dlp IG is broken)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from image_watermark_remove import clean_image_url
except ImportError:
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from image_watermark_remove import clean_image_url

import yaml

try:
    from instagram_digest_filters import build_telegram_caption, is_ad_post
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from instagram_digest_filters import build_telegram_caption, is_ad_post

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")

COOKIES = Path(os.environ.get("INSTAGRAM_COOKIES_PATH", "/root/instagram_cookies.txt"))
CONFIG = Path(os.environ.get("IG_CONFIG_OUT", "/root/config.instagram-mlbb.yaml"))
STATE = Path("/root/data/mlbb/instagram_digest_state.json")

try:
    from instagram_cookies_util import normalize_instagram_cookies_file
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from instagram_cookies_util import normalize_instagram_cookies_file


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}


def load_state() -> set[str]:
    if not STATE.exists():
        return set()
    try:
        return set(json.loads(STATE.read_text(encoding="utf-8")).get("published_ids", []))
    except Exception:
        return set()


def save_state(ids: set[str]) -> None:
    max_ids = int(os.environ.get("IG_STATE_MAX_IDS", "400"))
    trimmed = sorted(ids)
    if len(trimmed) > max_ids:
        trimmed = trimmed[-max_ids:]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps(
            {"published_ids": trimmed, "updated": time.strftime("%Y-%m-%d %H:%M:%S")},
            indent=2,
        ),
        encoding="utf-8",
    )


def notify_empty_digest(token: str, chat_id: str, published: set[str], errors: int) -> None:
    if os.environ.get("IG_NOTIFY_EMPTY", "1") != "1":
        return
    text = (
        "📷 Instagram-дайджест: новых постов для отправки нет.\n"
        f"В базе уже {len(published)} постов. "
        "Если давно не было картинок — обновите cookies (/ig_cookies) "
        "или напишите /ig_digest после обновления."
    )
    if errors:
        text += f"\n⚠️ Ошибок при загрузке: {errors}."
    tg_send(token, chat_id, "sendMessage", {"chat_id": chat_id, "text": text[:3900]})


def notify_cookies_expired(token: str, chat_id: str, detail: str = "") -> None:
    if os.environ.get("IG_NOTIFY_AUTH", "1") != "1":
        return
    text = (
        "⚠️ Instagram: сессия истекла (401 Unauthorized). "
        "Дайджест не может загрузить посты.\n\n"
        "1) Зайдите в instagram.com в браузере\n"
        "2) Экспорт cookies (Netscape) — Get cookies.txt LOCALLY\n"
        "3) Пришлите cookies.txt боту как документ\n"
        "4) /ig_digest — повторить рассылку"
    )
    if detail:
        text += f"\n\nТех.: {detail[:500]}"
    tg_send(token, chat_id, "sendMessage", {"chat_id": chat_id, "text": text[:3900]})


def _gallery_dl_entry_error(entry: object) -> str | None:
    if not isinstance(entry, list) or len(entry) < 2:
        return None
    meta = entry[1] if isinstance(entry[1], dict) else None
    if not meta or not meta.get("error"):
        return None
    msg = str(meta.get("message") or meta.get("error") or "")
    return msg[:800]


def _is_auth_error(message: str) -> bool:
    low = message.lower()
    return (
        "401" in message
        or "unauthorized" in low
        or ("login" in low and "required" in low)
    )


def _gallery_dl_env() -> dict[str, str]:
    """Instagram must not use the TikTok LTE proxy from .video_bot.env (often dead)."""
    env = os.environ.copy()
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        env.pop(key, None)
    ig_proxy = (os.environ.get("INSTAGRAM_PROXY_URL") or "").strip()
    if ig_proxy:
        env["HTTP_PROXY"] = ig_proxy
        env["HTTPS_PROXY"] = ig_proxy
    return env


def fetch_posts(username: str, limit: int) -> list[dict]:
    url = f"https://www.instagram.com/{username}/posts/"
    cmd = [
        "gallery-dl",
        "-j",
        "--cookies",
        str(COOKIES),
        "--range",
        f"1-{max(limit + 1, min(limit * 2, 6))}",
        url,
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env=_gallery_dl_env(),
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError((proc.stderr or proc.stdout or "gallery-dl failed")[-500:])
    raw = json.loads(proc.stdout or "[]")
    by_id: dict[str, dict] = {}
    fetch_errors: list[str] = []
    for entry in raw:
        err_msg = _gallery_dl_entry_error(entry)
        if err_msg:
            fetch_errors.append(err_msg)
            continue
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        meta = None
        for item in entry:
            if isinstance(item, dict) and (item.get("post_id") or item.get("post_shortcode")):
                meta = item
                break
        if meta is None:
            for item in reversed(entry):
                if isinstance(item, dict):
                    meta = item
                    break
        if not meta:
            continue
        post_id = str(meta.get("post_id") or meta.get("post_shortcode") or "")
        if not post_id:
            continue
        row = by_id.setdefault(
            post_id,
            {
                "post_id": post_id,
                "permalink": meta.get("post_url") or f"https://www.instagram.com/p/{meta.get('post_shortcode', '')}/",
                "caption": (meta.get("description") or "").strip(),
                "thumbnail": None,
                "username": meta.get("username") or username,
            },
        )
        if meta.get("description") and not row["caption"]:
            row["caption"] = str(meta["description"]).strip()
        if meta.get("post_url"):
            row["permalink"] = meta["post_url"]
        thumb = meta.get("display_url") or meta.get("thumbnail_url")
        if thumb and not row["thumbnail"]:
            row["thumbnail"] = thumb
        if isinstance(entry[1], str) and entry[1].startswith("http") and not row["thumbnail"]:
            row["thumbnail"] = entry[1]
    posts = list(by_id.values())
    posts.sort(key=lambda p: p["post_id"], reverse=True)
    if not posts and fetch_errors:
        last = fetch_errors[-1]
        if any(_is_auth_error(e) for e in fetch_errors):
            raise RuntimeError(f"instagram_auth_expired: {last}")
        raise RuntimeError(last)
    return posts[:limit]


def tg_send(token: str, chat_id: str, method: str, payload: dict) -> None:
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read().decode())
    if not result.get("ok"):
        raise RuntimeError(result)


def tg_send_photo_file(token: str, chat_id: str, image_path: Path, caption: str) -> None:
    cmd = [
        "curl",
        "-sS",
        "-m",
        "120",
        "-F",
        f"chat_id={chat_id}",
        "-F",
        f"caption={caption}",
        "-F",
        f"photo=@{image_path}",
        f"https://api.telegram.org/bot{token}/sendPhoto",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "sendPhoto failed")
    result = json.loads(proc.stdout or "{}")
    if not result.get("ok"):
        raise RuntimeError(result)


def publish(token: str, chat_id: str, source_name: str, post: dict) -> None:
    text = build_telegram_caption(source_name, post)
    thumb = post.get("thumbnail")
    temp_path: Path | None = None
    if thumb:
        try:
            if os.environ.get("IG_REMOVE_WATERMARK", "1") == "1":
                temp_path, cleaned = clean_image_url(thumb)
                if cleaned:
                    logging.info("watermark removed for %s", source_name)
                tg_send_photo_file(token, chat_id, temp_path, text)
            else:
                tg_send(token, chat_id, "sendPhoto", {"chat_id": chat_id, "photo": thumb, "caption": text})
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)
    else:
        tg_send(token, chat_id, "sendMessage", {"chat_id": chat_id, "text": text})


def main() -> int:
    if not COOKIES.exists():
        logging.error("cookies missing: %s", COOKIES)
        return 1
    try:
        normalize_instagram_cookies_file(COOKIES)
    except Exception as exc:
        logging.error("cookies invalid: %s", exc)
        return 1
    cfg = load_config()
    token = cfg["telegram"]["bot_token"]
    chat_id = str(cfg["telegram"]["channel_id"])
    max_posts = int(cfg.get("max_posts_per_run", 7))
    source_delay = float(os.environ.get("IG_DIGEST_SOURCE_DELAY_SEC", "10"))
    post_delay = float(os.environ.get("IG_DIGEST_POST_DELAY_SEC", "2.5"))
    scan_per_source = int(os.environ.get("IG_DIGEST_SCAN_PER_SOURCE", "3"))
    published = load_state()
    sent = 0
    skipped_ads = 0
    errors = 0
    auth_expired = False
    for source in cfg.get("instagram_sources") or []:
        if sent >= max_posts:
            break
        name = source.get("name") or source.get("url")
        username = str(source.get("url", "")).rstrip("/").split("/")[-1]
        try:
            posts = fetch_posts(username, scan_per_source)
            logging.info("fetch %s (%s): %d posts", name, username, len(posts))
        except Exception as exc:
            logging.warning("fetch %s failed: %s", name, exc)
            errors += 1
            if "instagram_auth_expired" in str(exc) or _is_auth_error(str(exc)):
                auth_expired = True
                break
            time.sleep(source_delay)
            continue
        if not posts:
            logging.warning("fetch %s: empty list", name)
        for post in posts:
            if sent >= max_posts:
                break
            pid = post["post_id"]
            if pid in published:
                continue
            is_ad, ad_reason = is_ad_post(post.get("caption", ""), post.get("thumbnail"))
            if is_ad:
                skipped_ads += 1
                logging.info("skip ad %s %s reason=%s", name, pid, ad_reason)
                continue
            try:
                publish(token, chat_id, str(name), post)
                published.add(pid)
                sent += 1
                logging.info("sent %s %s", name, pid)
                time.sleep(post_delay)
            except Exception as exc:
                logging.warning("telegram %s: %s", name, exc)
                errors += 1
        time.sleep(source_delay)
    save_state(published)
    logging.info(
        "done sent=%s skipped_ads=%s errors=%s auth_expired=%s",
        sent,
        skipped_ads,
        errors,
        auth_expired,
    )
    print(f"Published {sent} new posts.")
    if os.environ.get("IG_DIGEST_DRY_RUN", "0") != "1":
        if auth_expired:
            notify_cookies_expired(token, chat_id)
        elif sent == 0:
            notify_empty_digest(token, chat_id, published, errors)
    return 0 if sent > 0 else (1 if errors or auth_expired else 0)


if __name__ == "__main__":
    raise SystemExit(main())
