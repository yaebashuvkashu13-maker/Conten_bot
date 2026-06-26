#!/usr/bin/env python3
"""Instagram blogger digest via web API + cookies (yt-dlp IG extractor often broken)."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from http.cookiejar import MozillaCookieJar
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")

COOKIES = Path(os.environ.get("INSTAGRAM_COOKIES_PATH", "/root/instagram_cookies.txt"))
CONFIG = Path(os.environ.get("IG_CONFIG_OUT", "/root/config.instagram-mlbb.yaml"))
STATE = Path("/root/data/mlbb/instagram_digest_state.json")
APP_ID = "936619743392459"


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}


def load_state() -> set[str]:
    if not STATE.exists():
        return set()
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return set(data.get("published_ids", []))
    except Exception:
        return set()


def save_state(ids: set[str]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps({"published_ids": sorted(ids), "updated": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2),
        encoding="utf-8",
    )


def build_session():
    import requests

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "X-IG-App-ID": APP_ID,
            "X-ASBD-ID": "359341",
            "Referer": "https://www.instagram.com/",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    if COOKIES.exists():
        jar = MozillaCookieJar(str(COOKIES))
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(jar)
        csrf = session.cookies.get("csrftoken", domain=".instagram.com")
        if csrf:
            session.headers["X-CSRFToken"] = csrf
    proxy = os.environ.get("INSTAGRAM_PROXY_URL")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def fetch_profile_posts(session, username: str, limit: int = 2) -> list[dict]:
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={urllib.parse.quote(username)}"
    for attempt in range(3):
        resp = session.get(url, timeout=40)
        if resp.status_code == 429:
            time.sleep(8 * (attempt + 1))
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"profile {username}: HTTP {resp.status_code}")
        user = resp.json().get("data", {}).get("user") or {}
        edges = user.get("edge_owner_to_timeline_media", {}).get("edges") or []
        posts: list[dict] = []
        for edge in edges[:limit]:
            node = edge.get("node") or {}
            caption_edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
            caption = ""
            if caption_edges:
                caption = (caption_edges[0].get("node") or {}).get("text") or ""
            posts.append(
                {
                    "post_id": str(node.get("id") or node.get("shortcode") or ""),
                    "shortcode": node.get("shortcode") or "",
                    "permalink": f"https://www.instagram.com/p/{node.get('shortcode')}/",
                    "caption": caption,
                    "thumbnail": node.get("thumbnail_src") or node.get("display_url"),
                    "is_video": bool(node.get("is_video")),
                }
            )
        return posts
    raise RuntimeError(f"profile {username}: rate limited (429)")


def tg_send(token: str, chat_id: str, method: str, payload: dict) -> dict:
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
    return result


def publish_post(token: str, chat_id: str, source_name: str, post: dict) -> None:
    caption = (post.get("caption") or "Новый пост из Instagram").strip()
    text = f"📌 {source_name}\n{caption}\n\n{post.get('permalink', '')}"[:1024]
    thumb = post.get("thumbnail")
    if thumb:
        tg_send(token, chat_id, "sendPhoto", {"chat_id": chat_id, "photo": thumb, "caption": text})
    else:
        tg_send(token, chat_id, "sendMessage", {"chat_id": chat_id, "text": text})


def main() -> int:
    if not COOKIES.exists():
        logging.error("cookies missing: %s", COOKIES)
        return 1
    cfg = load_config()
    token = cfg["telegram"]["bot_token"]
    chat_id = str(cfg["telegram"]["channel_id"])
    max_posts = int(cfg.get("max_posts_per_run", 7))
    published_ids = load_state()
    session = build_session()
    sent = 0
    errors = 0
    for source in cfg.get("instagram_sources") or []:
        if sent >= max_posts:
            break
        name = source.get("name") or source.get("url")
        username = str(source.get("url", "")).rstrip("/").split("/")[-1]
        per_source = min(int(source.get("max_entries", 2)), max_posts - sent)
        try:
            posts = fetch_profile_posts(session, username, limit=per_source)
        except Exception as exc:
            logging.warning("fetch %s failed: %s", name, exc)
            errors += 1
            time.sleep(3)
            continue
        for post in posts:
            if sent >= max_posts:
                break
            pid = post.get("post_id") or post.get("shortcode")
            if not pid or pid in published_ids:
                continue
            try:
                publish_post(token, chat_id, str(name), post)
                published_ids.add(pid)
                sent += 1
                logging.info("sent %s %s", name, pid)
                time.sleep(1.5)
            except Exception as exc:
                logging.warning("telegram %s: %s", name, exc)
                errors += 1
        time.sleep(2)
    save_state(published_ids)
    logging.info("done sent=%s errors=%s", sent, errors)
    print(f"Published {sent} new posts.")
    return 0 if sent > 0 else (1 if errors else 0)


if __name__ == "__main__":
    try:
        import requests  # noqa: F401
    except ImportError:
        os.system("pip3 install --break-system-packages -q requests 2>/dev/null")
    raise SystemExit(main())
