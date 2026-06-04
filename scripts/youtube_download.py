#!/usr/bin/env python3
"""Download YouTube videos/playlists via yt-dlp (usually no proxy required)."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ENV_FILE = Path("/root/.video_bot.env")


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def is_youtube_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    if host not in {"youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"}:
        return False
    path = (parsed.path or "/").lower()
    if host == "youtu.be":
        return len(path) > 1
    return bool(
        path.startswith("/shorts/")
        or path.startswith("/live/")
        or path.startswith("/watch")
        or path.startswith(("/@", "/channel/", "/playlist"))
    )


def is_playlist_or_channel(url: str) -> bool:
    u = url.lower()
    if "list=" in u:
        return True
    return any(
        x in u
        for x in (
            "/channel/",
            "/@",
            "/c/",
            "/user/",
            "/playlist",
            "/live/",
        )
    )


def subprocess_env_no_proxy(base: dict[str, str] | None = None) -> dict[str, str]:
    """yt-dlp inherits HTTP_PROXY from os.environ — strip for direct YouTube."""
    out = (base or os.environ).copy()
    for key in list(out):
        if "proxy" in key.lower():
            out.pop(key, None)
    return out


def ytdlp_cmd(env: dict[str, str], *, use_proxy: bool = False) -> list[str]:
    impersonate = (env.get("YTDLP_IMPERSONATE") or "chrome-131").strip()
    cmd = ["yt-dlp", "--impersonate", impersonate, "--no-warnings", "--no-progress"]
    if use_proxy:
        proxy = env.get("YOUTUBE_PROXY") or env.get("YTDLP_PROXY") or env.get("SOCKS5_PROXY") or ""
        if proxy:
            cmd += ["--proxy", proxy]
    return cmd


def download_one(url: str, dest_dir: Path, env: dict[str, str] | None = None) -> Path:
    """Download single video to dest_dir; returns path to mp4."""
    env = env or load_env()
    dest_dir.mkdir(parents=True, exist_ok=True)
    template = dest_dir / "yt_%(id)s.%(ext)s"
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        "--no-playlist",
        "--restrict-filenames",
        "--merge-output-format",
        "mp4",
        "-f",
        env.get(
            "YOUTUBE_FORMAT",
            "bv*[height<=1080][vcodec^=avc1]+ba/b[height<=1080][vcodec^=avc1]/"
            "bv*[height<=1080]+ba/b[height<=1080]/b",
        ),
        "-o",
        str(template),
        url,
    ]
    subprocess.run(
        cmd,
        check=True,
        timeout=int(env.get("YOUTUBE_DOWNLOAD_TIMEOUT", "14400")),
        env=subprocess_env_no_proxy(),
    )
    files = sorted(dest_dir.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise RuntimeError(f"yt-dlp produced no mp4 for {url}")
    return files[0]


def download_feed(
    url: str,
    dest_dir: Path,
    *,
    max_videos: int = 5,
    env: dict[str, str] | None = None,
) -> list[Path]:
    """Download up to max_videos from channel / playlist."""
    env = env or load_env()
    dest_dir.mkdir(parents=True, exist_ok=True)
    template = dest_dir / "yt_%(id)s.%(ext)s"
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        "--restrict-filenames",
        "--merge-output-format",
        "mp4",
        "-f",
        env.get(
            "YOUTUBE_FORMAT",
            "bv*[height<=1080][vcodec^=avc1]+ba/b[height<=1080][vcodec^=avc1]/"
            "bv*[height<=1080]+ba/b[height<=1080]/b",
        ),
        "--playlist-end",
        str(max_videos),
        "-o",
        str(template),
        url,
    ]
    subprocess.run(
        cmd,
        check=True,
        timeout=int(env.get("YOUTUBE_FEED_TIMEOUT", "7200")),
        env=subprocess_env_no_proxy(),
    )
    return sorted(dest_dir.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)[
        :max_videos
    ]


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Download YouTube to folder")
    parser.add_argument("url")
    parser.add_argument("--out", type=Path, default=Path("/root/datasets/youtube/inbox"))
    parser.add_argument("--max", type=int, default=1)
    args = parser.parse_args()
    env = load_env()
    if args.max <= 1 and not is_playlist_or_channel(args.url):
        path = download_one(args.url, args.out, env)
        print(path)
    else:
        paths = download_feed(args.url, args.out, max_videos=args.max, env=env)
        for p in paths:
            print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
