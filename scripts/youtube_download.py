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
    from vod_env import load_env as _load

    return _load(path)


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


def cookies_preflight(env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Validate YouTube cookies before a download attempt.

    Returns (ok, reason). When cookies are not configured, ok=True (optional).
    When configured but missing/empty/stale, ok=False so callers can fail early.
    """
    if env is None:
        env = load_env()
    if env.get("YOUTUBE_COOKIES_PREFLIGHT", "1") != "1":
        return True, "preflight_disabled"
    cookies = (env.get("YOUTUBE_COOKIES_FILE") or env.get("YTDLP_COOKIES") or "").strip()
    if not cookies:
        if env.get("YOUTUBE_COOKIES_REQUIRED", "0") == "1":
            return False, "cookies_required_missing"
        return True, "cookies_not_configured"
    path = Path(cookies)
    if not path.is_file():
        return False, f"cookies_missing:{path}"
    try:
        size = path.stat().st_size
    except OSError as exc:
        return False, f"cookies_unreadable:{exc}"
    if size < 32:
        return False, "cookies_empty"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError as exc:
        return False, f"cookies_unreadable:{exc}"
    if "# Netscape HTTP Cookie File" not in text and "youtube.com" not in text.lower():
        return False, "cookies_format_invalid"
    max_age_h = float(env.get("YOUTUBE_COOKIES_MAX_AGE_HOURS", "336") or 336)
    if max_age_h > 0:
        age_h = (time.time() - path.stat().st_mtime) / 3600.0
        if age_h > max_age_h:
            return False, f"cookies_stale_hours={age_h:.0f}"
    return True, "cookies_ok"



def is_twitch_vod_url(url: str) -> bool:
    host = urlparse(url.strip()).netloc.lower().split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host in ("twitch.tv", "m.twitch.tv", "www.twitch.tv")


def normalize_twitch_url(url: str) -> str:
    raw = url.strip().rstrip(".,);")
    if raw.startswith("//"):
        raw = "https:" + raw
    elif not raw.startswith("http"):
        raw = "https://" + raw.lstrip("/")
    parsed = urlparse(raw)
    path = parsed.path.strip("/")
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "videos" and parts[1].isdigit():
        return f"https://www.twitch.tv/videos/{parts[1]}"
    return raw


def media_url_prefix(url: str) -> str:
    return "tw" if is_twitch_vod_url(url) else "yt"


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
        "b[height<=1080]/bv*[height<=1080]+ba/b[height<=1080]/b",
    )


def youtube_format_fallbacks(url: str, env: dict[str, str]) -> list[str]:
    primary = youtube_format_for_url(url, env)
    fallbacks = [
        env.get("YOUTUBE_FORMAT_FALLBACK", "18/b[height<=720]/bv*+ba/b"),
        "best[ext=mp4]/best",
        "b/bv*+ba/b",
    ]
    out = [primary]
    for fmt in fallbacks:
        if fmt and fmt not in out:
            out.append(fmt)
    return out


def ytdlp_match_filter(env: dict[str, str]) -> str:
    """yt-dlp --match-filter to skip long VODs masquerading as Shorts."""
    override = (env.get("YTDLP_MATCH_FILTER") or "").strip()
    if override:
        return override
    if env.get("MLBB_SHORTS_ONLY", "0") != "1":
        return ""
    max_d = float(env.get("MLBB_SHORTS_SHORT_MAX_SEC", env.get("MLBB_SHORTS_MAX_DURATION_SEC", "60")))
    min_d = float(env.get("MLBB_SHORTS_MIN_DURATION_SEC", "3"))
    return f"duration < {max_d} & duration > {min_d}"


def ytdlp_extra_args(env: dict[str, str]) -> list[str]:
    args = [
        "--socket-timeout",
        env.get("YOUTUBE_SOCKET_TIMEOUT", "45"),
        "--retries",
        env.get("YOUTUBE_RETRIES", "5"),
        "--fragment-retries",
        env.get("YOUTUBE_FRAGMENT_RETRIES", "10"),
    ]
    remote = (env.get("YTDLP_REMOTE_COMPONENTS") or "ejs:github").strip()
    if remote:
        args += ["--remote-components", remote]
    cookies = (env.get("YOUTUBE_COOKIES_FILE") or env.get("YTDLP_COOKIES") or "").strip()
    if cookies and Path(cookies).exists():
        args += ["--cookies", cookies]
    match_filter = ytdlp_match_filter(env)
    if match_filter:
        args += ["--match-filter", match_filter]
    return args


def subprocess_env_no_proxy(base: dict[str, str] | None = None) -> dict[str, str]:
    """Strip proxy vars but keep PATH and core runtime env for yt-dlp."""
    merged = {**os.environ, **(base or {})}
    out: dict[str, str] = {}
    for key, val in merged.items():
        if "proxy" in key.lower():
            continue
        out[key] = val
    path_parts = [
        out.get("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")),
        "/usr/local/bin",
        "/root/.deno/bin",
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for part in path_parts:
        for p in part.split(":"):
            p = p.strip()
            if p and p not in seen:
                seen.add(p)
                ordered.append(p)
    out["PATH"] = ":".join(ordered)
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


def _ytdlp_is_403(proc: subprocess.CompletedProcess[str]) -> bool:
    if proc.returncode == 0:
        return False
    err = f"{proc.stderr or ''}{proc.stdout or ''}"
    return "403" in err or "Forbidden" in err


def _ytdlp_should_retry_clients(proc: subprocess.CompletedProcess[str]) -> bool:
    if proc.returncode == 0:
        return False
    err = f"{proc.stderr or ''}{proc.stdout or ''}".lower()
    if "403" in err or "forbidden" in err:
        return True
    return "format is not available" in err or "only images are available" in err


def ytdlp_player_client_fallbacks(env: dict[str, str]) -> list[str]:
    cookies = (env.get("YOUTUBE_COOKIES_FILE") or env.get("YTDLP_COOKIES") or "").strip()
    if cookies and Path(cookies).exists():
        # android/ios skip when cookies are set — web+mweb first.
        raw = env.get("YTDLP_PLAYER_CLIENTS", "web,mweb,tv,android,ios")
    else:
        raw = env.get("YTDLP_PLAYER_CLIENTS", "android,web,ios,mweb")
    return [c.strip() for c in raw.split(",") if c.strip()]


def _inject_player_client(cmd: list[str], client: str) -> list[str]:
    out = [arg for arg in cmd if not str(arg).startswith("youtube:player_client=")]
    for i, arg in enumerate(out):
        if arg == "--extractor-args" and i + 1 < len(out):
            merged = f"{out[i + 1]},youtube:player_client={client}"
            return out[:i] + ["--extractor-args", merged] + out[i + 2 :]
    return out + ["--extractor-args", f"youtube:player_client={client}"]


def run_ytdlp(
    cmd: list[str],
    env: dict[str, str],
    *,
    timeout: float = 180,
    label: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run yt-dlp; on HTTP 403 retry with alternate YouTube player clients."""
    base_env = subprocess_env_no_proxy(env)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=base_env,
    )
    if proc.returncode == 0:
        return proc
    if not _ytdlp_should_retry_clients(proc):
        return proc
    retry_delay = float(env.get("YTDLP_403_RETRY_DELAY", "4"))
    for client in ytdlp_player_client_fallbacks(env):
        time.sleep(retry_delay)
        retry_cmd = _inject_player_client(cmd, client)
        proc = subprocess.run(
            retry_cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=base_env,
        )
        if proc.returncode == 0:
            return proc
        if not _ytdlp_should_retry_clients(proc):
            break
    return proc


def download_one(url: str, dest_dir: Path, env: dict[str, str] | None = None) -> Path:
    env = {**os.environ, **(env or load_env())}
    try:
        from youtube_source_health import classify_download_error, is_blocked, record_download_result
    except ImportError:
        classify_download_error = None
        is_blocked = None
        record_download_result = None
    if is_blocked is not None:
        blocked, reason = is_blocked(url=url)
        if blocked:
            raise RuntimeError(f"youtube_source_blocked: {reason}")
    if not is_twitch_vod_url(url):
        ok_cookies, cookie_reason = cookies_preflight(env)
        if not ok_cookies:
            if record_download_result is not None:
                record_download_result(url=url, ok=False, error_kind="auth")
            raise RuntimeError(f"youtube_cookies_preflight_failed: {cookie_reason}")
    if is_twitch_vod_url(url):
        url = normalize_twitch_url(url)
        prefix = "tw"
        formats = [
            env.get("TWITCH_FORMAT", "bv*[height<=1080]+ba/b[height<=1080]/b"),
        ]
    else:
        url = normalize_youtube_url(url)
        prefix = "yt"
        formats = youtube_format_fallbacks(url, env)
    dest_dir.mkdir(parents=True, exist_ok=True)
    template = dest_dir / f"{prefix}_%(id)s.%(ext)s"
    last_err = ""
    for fmt in formats:
        cmd = ytdlp_cmd(env) + [
            "--no-playlist",
            "--restrict-filenames",
            "--merge-output-format",
            "mp4",
            "-f",
            fmt,
            *ytdlp_extra_args(env),
            "-o",
            str(template),
            url,
        ]
        proc = run_ytdlp(
            cmd,
            env,
            timeout=int(env.get("YOUTUBE_DOWNLOAD_TIMEOUT", "14400")),
            label=f"download-{prefix}",
        )
        if proc.returncode == 0:
            break
        last_err = (proc.stderr or proc.stdout or "")[:800]
        if "format is not available" in last_err.lower():
            continue
        if _ytdlp_should_retry_clients(proc):
            continue
        if record_download_result is not None and classify_download_error is not None:
            record_download_result(url=url, ok=False, error_kind=classify_download_error(last_err))
        raise RuntimeError(f"yt-dlp download failed rc={proc.returncode}: {last_err}")
    else:
        if record_download_result is not None and classify_download_error is not None:
            record_download_result(url=url, ok=False, error_kind=classify_download_error(last_err))
        raise RuntimeError(f"yt-dlp download failed: {last_err}")
    files = sorted(dest_dir.glob(f"{prefix}_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        if record_download_result is not None:
            record_download_result(url=url, ok=False, error_kind="quality")
        raise RuntimeError(f"yt-dlp produced no mp4 for {url}")
    if record_download_result is not None:
        record_download_result(url=url, ok=True)
    return files[0]
