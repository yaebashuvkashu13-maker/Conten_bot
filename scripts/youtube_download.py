#!/usr/bin/env python3
"""Download YouTube videos/playlists via yt-dlp."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ENV_FILE = Path("/root/.video_bot.env")


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def ytdlp_bin(env: dict[str, str] | None = None) -> str:
    env = env or {}
    for candidate in (
        env.get("YTDLP_BIN", "").strip(),
        shutil.which("yt-dlp") or "",
        "/usr/local/bin/yt-dlp",
        "/usr/bin/yt-dlp",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return "yt-dlp"


def normalize_youtube_url(url: str) -> str:
    raw = url.strip().rstrip(".,);")
    if not raw:
        return raw
    if raw.startswith("//"):
        raw = "https:" + raw
    elif re.match(r"^(?:www\.)?(?:youtube\.com|youtu\.be|m\.youtube\.com)/", raw, re.I):
        raw = "https://" + raw.lstrip("/")

    parsed = urlparse(raw)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or ""
    vid = None
    if host == "youtu.be":
        vid = path.strip("/").split("/")[0][:11]
    else:
        low = path.lower()
        for marker in ("/shorts/", "/live/", "/embed/", "/v/"):
            if marker in low:
                vid = low.split(marker, 1)[-1].split("/")[0].split("?")[0][:11]
                break
        if not vid and path.startswith("/watch"):
            vid = (parse_qs(parsed.query).get("v") or [""])[0][:11]
    if vid and len(vid) == 11:
        return f"https://www.youtube.com/watch?v={vid}"
    return raw


def is_youtube_shorts_url(url: str) -> bool:
    return "/shorts/" in urlparse(url).path.lower()


def youtube_format_for_url(url: str, env: dict[str, str]) -> str:
    if is_youtube_shorts_url(url):
        return env.get("YOUTUBE_SHORTS_FORMAT", "bv*[height<=1080]+ba/b[height<=720]/b")
    return env.get(
        "YOUTUBE_FORMAT",
        "bv*[height<=1080][vcodec^=avc1]+ba/b[height<=1080][vcodec^=avc1]/"
        "bv*[height<=1080]+ba/b[height<=1080]/b",
    )


def ytdlp_extra_args(env: dict[str, str]) -> list[str]:
    args = [
        "--socket-timeout",
        env.get("YOUTUBE_SOCKET_TIMEOUT", "45"),
        "--retries",
        env.get("YOUTUBE_RETRIES", "5"),
        "--fragment-retries",
        env.get("YOUTUBE_FRAGMENT_RETRIES", "10"),
    ]
    cookies = (env.get("YOUTUBE_COOKIES_FILE") or env.get("YTDLP_COOKIES") or "").strip()
    if cookies and Path(cookies).exists():
        args += ["--cookies", cookies]
    return args


def subprocess_env_no_proxy(base: dict[str, str] | None = None) -> dict[str, str]:
    """Strip proxy vars but keep PATH and core runtime env for yt-dlp."""
    merged = {**os.environ, **(base or {})}
    out: dict[str, str] = {}
    for key, val in merged.items():
        if "proxy" in key.lower():
            continue
        out[key] = val
    if "PATH" not in out:
        out["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    return out


def _proxy_url(env: dict[str, str]) -> str:
    return (env.get("YOUTUBE_PROXY") or env.get("YTDLP_PROXY") or env.get("SOCKS5_PROXY") or "").strip()


def _proxy_alive(proxy: str, *, timeout: float = 4.0) -> bool:
    if not proxy:
        return False
    try:
        import socket
        from urllib.parse import urlparse

        parsed = urlparse(proxy)
        host = parsed.hostname
        port = parsed.port or 1080
        if not host:
            return False
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except OSError:
        return False


def ytdlp_use_proxy(env: dict[str, str]) -> bool:
    if env.get("MLBB_YTDLP_USE_PROXY", env.get("YTDLP_USE_PROXY", "0")) == "1":
        return _proxy_alive(_proxy_url(env))
    return False


def ytdlp_cmd(env: dict[str, str], *, use_proxy: bool | None = None) -> list[str]:
    impersonate = (env.get("YTDLP_IMPERSONATE") or "chrome-131").strip()
    cmd = [ytdlp_bin(env), "--impersonate", impersonate, "--no-warnings", "--no-progress"]
    if use_proxy is None:
        use_proxy = ytdlp_use_proxy(env)
    if use_proxy:
        proxy = _proxy_url(env)
        if proxy and _proxy_alive(proxy):
            cmd += ["--proxy", proxy]
    return cmd


def download_one(url: str, dest_dir: Path, env: dict[str, str] | None = None) -> Path:
    env = {**os.environ, **(env or load_env())}
    url = normalize_youtube_url(url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    template = dest_dir / "yt_%(id)s.%(ext)s"
    cmd = ytdlp_cmd(env) + [
        "--no-playlist",
        "--restrict-filenames",
        "--merge-output-format",
        "mp4",
        "-f",
        youtube_format_for_url(url, env),
        *ytdlp_extra_args(env),
        "-o",
        str(template),
        url,
    ]
    subprocess.run(
        cmd,
        check=True,
        timeout=int(env.get("YOUTUBE_DOWNLOAD_TIMEOUT", "14400")),
        env=subprocess_env_no_proxy(env),
    )
    files = sorted(dest_dir.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise RuntimeError(f"yt-dlp produced no mp4 for {url}")
    return files[0]
