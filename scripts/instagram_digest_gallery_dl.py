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

import yaml

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")

COOKIES = Path(os.environ.get("INSTAGRAM_COOKIES_PATH", "/root/instagram_cookies.txt"))
CONFIG = Path(os.environ.get("IG_CONFIG_OUT", "/root/config.instagram-mlbb.yaml"))
STATE = Path("/root/data/mlbb/instagram_digest_state.json")


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
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps({"published_ids": sorted(ids), "updated": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2),
        encoding="utf-8",
    )


def fetch_posts(username: str, limit: int) -> list[dict]:
    url = f"https://www.instagram.com/{username}/posts/"
    cmd = [
        "gallery-dl",
        "-j",
        "--cookies",
        str(COOKIES),
        "--range",
        f"1-{max(limit * 4, 4)}",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError((proc.stderr or proc.stdout or "gallery-dl failed")[-500:])
    raw = json.loads(proc.stdout or "[]")
    by_id: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, list) or len(entry) < 3:
            continue
        meta = entry[2]
        if not isinstance(meta, dict):
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


def publish(token: str, chat_id: str, source_name: str, post: dict) -> None:
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
    published = load_state()
    sent = 0
    errors = 0
    for source in cfg.get("instagram_sources") or []:
        if sent >= max_posts:
            break
        name = source.get("name") or source.get("url")
        username = str(source.get("url", "")).rstrip("/").split("/")[-1]
        per_source = min(int(source.get("max_entries", 1)), max_posts - sent)
        try:
            posts = fetch_posts(username, per_source)
        except Exception as exc:
            logging.warning("fetch %s failed: %s", name, exc)
            errors += 1
            time.sleep(4)
            continue
        for post in posts:
            if sent >= max_posts:
                break
            pid = post["post_id"]
            if pid in published:
                continue
            try:
                publish(token, chat_id, str(name), post)
                published.add(pid)
                sent += 1
                logging.info("sent %s %s", name, pid)
                time.sleep(1.4)
            except Exception as exc:
                logging.warning("telegram %s: %s", name, exc)
                errors += 1
        time.sleep(3)
    save_state(published)
    logging.info("done sent=%s errors=%s", sent, errors)
    print(f"Published {sent} new posts.")
    return 0 if sent > 0 else (1 if errors else 0)


if __name__ == "__main__":
    raise SystemExit(main())
