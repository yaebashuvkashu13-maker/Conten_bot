#!/usr/bin/env python3
"""Gentle PUBG Mobile TikTok harvest (human-like, small batches)."""

from __future__ import annotations

import json
import random
import subprocess
import time
from pathlib import Path

OUT = Path("/root/datasets/tiktok/pubg")
LOG = Path("/root/data/pubg/tiktok_download.log")
ENV = Path("/root/.video_bot.env")
SEARCHES = [
    "pubg mobile gameplay",
    "pubg mobile highlights",
    "pubg mobile clutch",
]
SESSION_LIMIT = 12


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV.exists():
        return env
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    env = load_env()
    proxy = env.get("YTDLP_PROXY") or env.get("SOCKS5_PROXY") or ""
    if not proxy:
        log("no proxy")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    for query in SEARCHES:
        if downloaded >= SESSION_LIMIT:
            break
        log(f"search {query}")
        cmd = [
            "yt-dlp",
            "--proxy",
            proxy,
            "--flat-playlist",
            "--print",
            "webpage_url",
            f"tiktoksearch20:{query}",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            continue
        urls = [u.strip() for u in result.stdout.splitlines() if "/video/" in u]
        random.shuffle(urls)
        for url in urls[:6]:
            if downloaded >= SESSION_LIMIT:
                break
            vid = url.rstrip("/").split("/")[-1]
            dest = OUT / f"{vid}.mp4"
            if dest.exists() and dest.stat().st_size > 80_000:
                continue
            dl = [
                "yt-dlp",
                "--proxy",
                proxy,
                "--no-playlist",
                "-f",
                "best[height<=720]/best",
                "-o",
                str(dest.with_suffix(".part")),
                url,
            ]
            try:
                subprocess.run(dl, check=True, capture_output=True, timeout=180)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                dest.with_suffix(".part").unlink(missing_ok=True)
                continue
            part = dest.with_suffix(".mp4.part")
            if part.exists():
                part.replace(dest)
            if dest.exists():
                downloaded += 1
                log(f"saved {dest.name}")
            time.sleep(random.uniform(14, 32))
        time.sleep(random.uniform(20, 45))
    log(json.dumps({"downloaded": downloaded, "total": len(list(OUT.glob("*.mp4")))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
