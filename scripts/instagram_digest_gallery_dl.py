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
    cfg = load_config()
    token = cfg["telegram"]["bot_token"]
    chat_id = str(cfg["telegram"]["channel_id"])
    max_posts = int(cfg.get("max_posts_per_run", 7))
    scan_per_source = int(os.environ.get("IG_DIGEST_SCAN_PER_SOURCE", "6"))
    published = load_state()
    sent = 0
    skipped_ads = 0
    errors = 0
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
            time.sleep(4)
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
                time.sleep(1.4)
            except Exception as exc:
                logging.warning("telegram %s: %s", name, exc)
                errors += 1
        time.sleep(3)
    save_state(published)
    logging.info("done sent=%s skipped_ads=%s errors=%s", sent, skipped_ads, errors)
    print(f"Published {sent} new posts.")
    if sent == 0 and os.environ.get("IG_DIGEST_DRY_RUN", "0") != "1":
        notify_empty_digest(token, chat_id, published, errors)
    return 0 if sent > 0 else (1 if errors else 0)


if __name__ == "__main__":
    raise SystemExit(main())
