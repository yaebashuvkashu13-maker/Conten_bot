#!/usr/bin/env python3
"""Multi-platform short-video harvest for PUBG Metro silver learning.

Sources (gentle, rate-limited):
  - TikTok user feeds (yt-dlp `tiktok:user`; tag/search extractors are broken)
  - Instagram Reels / tags (requires INSTAGRAM_COOKIES_PATH)
  - VK clips (optional; often broken in current yt-dlp — reported clearly)

Downloaded files land under /root/datasets/{tiktok,instagram,vk}/pubg/
and are scored by pubg_shorts_autolearn local pool (no extra YouTube pressure).

Proxy: use only when the SOCKS/HTTP endpoint accepts TCP; otherwise go direct
(TikTok works from the VPS without the external SOCKS).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from youtube_download import load_env, ytdlp_bin

REPO = Path(os.environ.get("CONTENT_BOT_REPO", Path(__file__).resolve().parent.parent))
ENV_PATH = Path("/root/.video_bot.env")
STATE_PATH = Path(
    os.environ.get(
        "PUBG_MULTI_HARVEST_STATE",
        "/root/data/pubg/shorts_autolearn/multi_harvest_state.json",
    )
)

# Seed Metro / PUBG Mobile creators. Override with PUBG_TIKTOK_USERS=a,b,c
TIKTOK_USERS = (
    "metroroyale",
    "pubgmobile",
    "pubgm.official",
    "pubgmobilesports",
    "pubgmobileesports",
    "officialpubgmobile",
    "predator.pubgmobile",
    "sparrowpubg3",
    "dabplays",
)

# Prefer gameplay-ish titles when harvesting broad PUBG accounts.
METRO_TITLE_RE = re.compile(
    r"metro|метро|royale|роял|clutch|перестрел|fight|бой|loot|эвакуац|squad|"
    r"kill|килл|highlight|геймплей|gameplay|brawl|дуэль",
    re.I,
)

INSTAGRAM_TAGS = (
    "metrroyale",
    "metroroyale",
    "pubgmetrroyale",
    "pubgmobilemetro",
)

VK_VIDEO_URLS = (
    # Direct video / wall posts work more often than search extractors.
    # Keep empty-safe: harvest_vk probes these if set via env.
)

# Screen names / channel URLs — enough; no per-video links needed.
# Clips tabs on these communities are PUBG-only (owner-confirmed).
VK_CHANNELS = (
    "pubgkotleta",
    "club239893669",  # Pubg kotleta
    "club201271677",  # PUBG mobile metro
    "pubgmgasper0",
    "metroshop_pubg1",
)

VK_CHANNEL_OWNER_IDS = {
    "pubgkotleta": -230808874,
    "club239893669": -239893669,
    "club201271677": -201271677,
    "pubgmgasper0": -228773005,
    "metroshop_pubg1": -213549615,
}


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _proxy(env: dict[str, str]) -> str:
    direct = (
        env.get("YTDLP_PROXY")
        or env.get("SOCKS5_PROXY")
        or env.get("HTTPS_PROXY")
        or os.environ.get("YTDLP_PROXY")
        or ""
    ).strip()
    if direct:
        return direct

    def _from_parts(src: dict[str, str]) -> str:
        host = (src.get("SOCKS_HOST") or "").strip()
        port = (src.get("SOCKS_PORT") or "").strip()
        user = (src.get("SOCKS_USER") or "").strip()
        password = (src.get("SOCKS_PASS") or "").strip()
        if not (host and port):
            return ""
        if user and password:
            return f"socks5://{user}:{password}@{host}:{port}"
        return f"socks5://{host}:{port}"

    built = _from_parts({**os.environ, **env})
    if built:
        return built
    cred = Path(os.environ.get("PROXY_CREDS_FILE", "/root/proxy-creds.env"))
    if cred.is_file():
        try:
            from youtube_download import load_env as _load

            return _from_parts(_load(cred))
        except Exception:
            return ""
    return ""


def _proxy_alive(proxy: str, *, timeout: float = 3.0) -> bool:
    if not proxy:
        return False
    try:
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


def _use_proxy(env: dict[str, str], *, prefer: bool) -> bool:
    """Only attach --proxy when the endpoint accepts TCP."""
    if not prefer:
        return False
    force = os.environ.get("PUBG_MULTI_FORCE_PROXY", env.get("PUBG_MULTI_FORCE_PROXY", "0"))
    proxy = _proxy(env)
    if not proxy:
        return False
    if force == "1":
        return True
    return _proxy_alive(proxy)


def _cookies() -> Path | None:
    for cand in (
        os.environ.get("INSTAGRAM_COOKIES_PATH", "").strip(),
        "/root/instagram_cookies.txt",
        "/root/data/instagram_cookies.txt",
        str(REPO / "instagram_cookies.txt"),
    ):
        if cand and Path(cand).is_file() and Path(cand).stat().st_size > 50:
            return Path(cand)
    return None


def load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"seen_urls": [], "downloaded": 0, "by_source": {}, "tiktok_users_ok": []}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {"seen_urls": [], "downloaded": 0, "by_source": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    seen = list(state.get("seen_urls") or [])
    if len(seen) > 8000:
        state["seen_urls"] = seen[-8000:]
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _ytdlp_base(env: dict[str, str], *, use_proxy: bool, cookies: Path | None = None) -> list[str]:
    cmd = [ytdlp_bin(env)]
    if use_proxy:
        proxy = _proxy(env)
        if proxy:
            cmd += ["--proxy", proxy]
    if cookies is not None:
        cmd += ["--cookies", str(cookies)]
    cmd += [
        "--no-playlist",
        "--retries",
        "2",
        "--fragment-retries",
        "3",
        "--socket-timeout",
        "30",
    ]
    return cmd


def _list_urls(
    target: str,
    *,
    env: dict[str, str],
    use_proxy: bool,
    cookies: Path | None = None,
    limit: int = 15,
    playlist: bool = True,
) -> list[str]:
    cmd = _ytdlp_base(env, use_proxy=use_proxy, cookies=cookies)
    # User/tag feeds need playlist mode; single videos do not.
    if playlist:
        cmd = [c for c in cmd if c != "--no-playlist"]
        cmd += ["--flat-playlist", "--playlist-end", str(max(limit, 1))]
    cmd += ["--print", "%(webpage_url)s\t%(title)s", target]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=150)
    except subprocess.TimeoutExpired:
        return []
    rows: list[tuple[str, str]] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("http"):
            continue
        if "\t" in line:
            url, title = line.split("\t", 1)
        else:
            url, title = line, ""
        rows.append((url.strip(), title.strip()))
        if len(rows) >= limit:
            break
    return [u for u, _ in rows]  # titles filtered by caller via _list_urls_filtered


def _list_urls_filtered(
    target: str,
    *,
    env: dict[str, str],
    use_proxy: bool,
    cookies: Path | None = None,
    limit: int = 15,
    require_metro: bool = False,
) -> list[str]:
    cmd = _ytdlp_base(env, use_proxy=use_proxy, cookies=cookies)
    cmd = [c for c in cmd if c != "--no-playlist"]
    cmd += [
        "--flat-playlist",
        "--playlist-end",
        str(max(limit * 3, 12)),
        "--print",
        "%(webpage_url)s\t%(title)s",
        target,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=150)
    except subprocess.TimeoutExpired:
        return []
    picked: list[str] = []
    loose: list[str] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("http"):
            continue
        if "\t" in line:
            url, title = line.split("\t", 1)
        else:
            url, title = line, ""
        url = url.strip()
        if "/video/" not in url and "/reel/" not in url and "/clip" not in url.lower():
            # Keep TikTok video URLs; skip live / non-video.
            if "tiktok.com" in url and "/video/" not in url:
                continue
        if METRO_TITLE_RE.search(title or ""):
            picked.append(url)
        else:
            loose.append(url)
        if len(picked) >= limit:
            break
    if require_metro:
        return picked[:limit]
    # Prefer metro-titled; fill remainder from the rest.
    out = picked + [u for u in loose if u not in picked]
    return out[:limit]


def _download(
    url: str,
    dest: Path,
    *,
    env: dict[str, str],
    use_proxy: bool,
    cookies: Path | None = None,
) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 40_000:
        return True
    out_tmpl = str(dest.with_suffix("")) + ".%(ext)s"
    cmd = _ytdlp_base(env, use_proxy=use_proxy, cookies=cookies) + [
        "-f",
        "b[height<=720]/bv*[height<=720]+ba/b",
        "--merge-output-format",
        "mp4",
        "-o",
        out_tmpl,
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=240)
    except subprocess.TimeoutExpired:
        return False
    if proc.returncode != 0:
        return False
    if dest.is_file() and dest.stat().st_size > 40_000:
        return True
    stem = dest.with_suffix("").name
    cands = sorted(
        [
            p
            for p in dest.parent.glob(f"{stem}.*")
            if p.suffix.lower() in {".mp4", ".webm", ".mkv"} and p.stat().st_size > 40_000
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not cands:
        return False
    if cands[0] != dest:
        try:
            if dest.exists():
                dest.unlink()
            cands[0].replace(dest)
        except OSError:
            return cands[0].stat().st_size > 40_000
    return dest.is_file() and dest.stat().st_size > 40_000


def _id_from_url(url: str, source: str) -> str:
    tail = url.rstrip("/").split("/")[-1]
    tail = re.sub(r"[^A-Za-z0-9_-]", "", tail)[:32]
    if len(tail) >= 6:
        return f"{source[:2]}_{tail}"
    import hashlib

    return f"{source[:2]}_{hashlib.sha1(url.encode()).hexdigest()[:12]}"


def _tiktok_users(env: dict[str, str]) -> list[str]:
    raw = (env.get("PUBG_TIKTOK_USERS") or os.environ.get("PUBG_TIKTOK_USERS") or "").strip()
    if raw:
        return [u.strip().lstrip("@") for u in raw.split(",") if u.strip()]
    return list(TIKTOK_USERS)


def harvest_tiktok(env: dict[str, str], state: dict[str, Any], *, limit: int) -> dict[str, Any]:
    """Pull short videos from curated TikTok user feeds (no broken search/tag)."""
    out_dir = Path(os.environ.get("PUBG_TIKTOK_DIR", "/root/datasets/tiktok/pubg"))
    out_dir.mkdir(parents=True, exist_ok=True)
    prefer_proxy = os.environ.get("PUBG_TIKTOK_PREFER_PROXY", env.get("PUBG_TIKTOK_PREFER_PROXY", "0")) == "1"
    use_proxy = _use_proxy(env, prefer=prefer_proxy or bool(_proxy(env)))
    # If configured proxy is dead, fall back to direct (do not hard-fail).
    if prefer_proxy and not use_proxy and _proxy(env):
        use_proxy = False
    # Default: try direct first for TikTok (VPS reaches TikTok; external SOCKS often dead).
    if os.environ.get("PUBG_TIKTOK_DIRECT", env.get("PUBG_TIKTOK_DIRECT", "1")) == "1":
        use_proxy = False

    seen = set(state.get("seen_urls") or [])
    saved = 0
    attempted = 0
    errors: list[str] = []
    users = _tiktok_users(env)
    random.shuffle(users)
    users_ok: list[str] = list(state.get("tiktok_users_ok") or [])

    for user in users:
        if saved >= limit:
            break
        # Metro-named accounts: take any video; broad PUBG: prefer metro-ish titles.
        require_metro = user not in {"metroroyale", "pubgmetroroyale"} and "metro" not in user.lower()
        target = f"https://www.tiktok.com/@{user}"
        urls = _list_urls_filtered(
            target,
            env=env,
            use_proxy=use_proxy,
            limit=max(8, limit * 2),
            require_metro=require_metro,
        )
        if not urls and require_metro:
            # Soften: still take a couple from the profile for silver diversity.
            urls = _list_urls_filtered(
                target,
                env=env,
                use_proxy=use_proxy,
                limit=4,
                require_metro=False,
            )
        if urls and user not in users_ok:
            users_ok.append(user)
        random.shuffle(urls)
        time.sleep(random.uniform(4, 10))
        for url in urls:
            if saved >= limit:
                break
            if url in seen:
                continue
            if "/video/" not in url:
                continue
            attempted += 1
            vid = _id_from_url(url, "tt")
            dest = out_dir / f"{vid}.mp4"
            ok = _download(url, dest, env=env, use_proxy=use_proxy)
            seen.add(url)
            if ok:
                saved += 1
                state["downloaded"] = int(state.get("downloaded") or 0) + 1
                by = dict(state.get("by_source") or {})
                by["tiktok"] = int(by.get("tiktok") or 0) + 1
                state["by_source"] = by
            else:
                errors.append(f"tt_fail:{vid}")
            time.sleep(random.uniform(10, 24))

    state["seen_urls"] = list(seen)
    state["tiktok_users_ok"] = users_ok[-40:]
    return {
        "saved": saved,
        "attempted": attempted,
        "errors": errors[:10],
        "out": str(out_dir),
        "via": "users",
        "proxy": use_proxy,
        "users_tried": users[:8],
    }


def harvest_instagram(env: dict[str, str], state: dict[str, Any], *, limit: int) -> dict[str, Any]:
    cookies = _cookies()
    if cookies is None:
        return {
            "saved": 0,
            "skipped": "no_instagram_cookies",
            "hint": "Положи cookies в /root/instagram_cookies.txt (Netscape) — тогда Reels поедут",
            "attempted": 0,
        }
    out_dir = Path(os.environ.get("PUBG_INSTAGRAM_DIR", "/root/datasets/instagram/pubg"))
    out_dir.mkdir(parents=True, exist_ok=True)
    seen = set(state.get("seen_urls") or [])
    saved = 0
    attempted = 0
    errors: list[str] = []
    tags = list(INSTAGRAM_TAGS)
    random.shuffle(tags)
    use_proxy = _use_proxy(env, prefer=True)
    for tag in tags:
        if saved >= limit:
            break
        query = f"https://www.instagram.com/explore/tags/{tag}/"
        urls = _list_urls(query, env=env, use_proxy=use_proxy, cookies=cookies, limit=10)
        random.shuffle(urls)
        time.sleep(random.uniform(10, 22))
        for url in urls:
            if saved >= limit:
                break
            if "/reel/" not in url and "/reels/" not in url and "/p/" not in url:
                continue
            if url in seen:
                continue
            attempted += 1
            vid = _id_from_url(url, "ig")
            dest = out_dir / f"{vid}.mp4"
            ok = _download(url, dest, env=env, use_proxy=use_proxy, cookies=cookies)
            seen.add(url)
            if ok:
                saved += 1
                state["downloaded"] = int(state.get("downloaded") or 0) + 1
                by = dict(state.get("by_source") or {})
                by["instagram"] = int(by.get("instagram") or 0) + 1
                state["by_source"] = by
            else:
                errors.append(f"ig_fail:{vid}")
            time.sleep(random.uniform(18, 40))
    state["seen_urls"] = list(seen)
    return {"saved": saved, "attempted": attempted, "errors": errors[:10], "out": str(out_dir)}


def _vk_cookies() -> Path | None:
    for cand in (
        os.environ.get("VK_COOKIES_PATH", "").strip(),
        "/root/vk_cookies.txt",
        "/root/data/vk_cookies.txt",
        str(REPO / "vk_cookies.txt"),
    ):
        if cand and Path(cand).is_file() and Path(cand).stat().st_size > 50:
            return Path(cand)
    return None


def _vk_user_token(env: dict[str, str]) -> str:
    """User OAuth token (video scope). Group/community tokens cannot list foreign clips."""
    for key in (
        "PUBG_VK_ACCESS_TOKEN",
        "VK_USER_ACCESS_TOKEN",
        "VK_ACCESS_TOKEN",
    ):
        tok = (env.get(key) or os.environ.get(key) or "").strip()
        if tok:
            return tok
    return ""


def _vk_channels(env: dict[str, str]) -> list[str]:
    raw = (env.get("PUBG_VK_CHANNELS") or os.environ.get("PUBG_VK_CHANNELS") or "").strip()
    if raw:
        out: list[str] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            # Accept full URL or screen name.
            part = part.rstrip("/").split("/")[-1]
            if part.startswith("clips"):
                continue
            out.append(part.lstrip("@"))
        return out or list(VK_CHANNELS)
    return list(VK_CHANNELS)


def _vk_api(method: str, params: dict[str, Any], *, token: str) -> dict[str, Any]:
    import urllib.parse
    import urllib.request

    q = {**params, "access_token": token, "v": "5.199"}
    url = "https://api.vk.com/method/" + method + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 content-bot"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _vk_owner_id(screen: str, *, token: str) -> int | None:
    key = screen.strip().lstrip("@").lower()
    if key in VK_CHANNEL_OWNER_IDS:
        return int(VK_CHANNEL_OWNER_IDS[key])
    # club123456 / public123456 without API.
    m = re.fullmatch(r"(?:club|public)(\d+)", key)
    if m:
        return -int(m.group(1))
    if key.isdigit():
        return -int(key)
    if not token:
        return None
    try:
        data = _vk_api("utils.resolveScreenName", {"screen_name": key}, token=token)
        resp = data.get("response") or {}
        if not isinstance(resp, dict) or not resp.get("object_id"):
            return None
        oid = int(resp["object_id"])
        if resp.get("type") in {"group", "page", "event"}:
            return -oid
        return oid
    except Exception:
        return None


def _vk_list_channel_urls(owner_id: int, *, token: str, limit: int) -> list[str]:
    """List clip/video page URLs for a channel via user token."""
    urls: list[str] = []
    # Prefer short videos (Clips tab).
    for method, params in (
        ("shortVideo.getOwnerVideos", {"owner_id": owner_id, "count": max(limit * 3, 20)}),
        ("video.get", {"owner_id": owner_id, "count": max(limit * 3, 20)}),
    ):
        try:
            data = _vk_api(method, params, token=token)
        except Exception:
            continue
        if data.get("error"):
            continue
        resp = data.get("response") or {}
        items = resp.get("items") if isinstance(resp, dict) else resp
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            oid = item.get("owner_id", owner_id)
            vid = item.get("id") or item.get("video_id")
            if oid is None or vid is None:
                continue
            # Clips use /clip{owner}_{id}; regular videos /video{owner}_{id}.
            kind = "clip" if method.startswith("shortVideo") or item.get("type") == "short_video" else "video"
            urls.append(f"https://vk.com/{kind}{oid}_{vid}")
            if len(urls) >= limit * 4:
                break
        if urls:
            break
    # De-dupe preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def harvest_vk(env: dict[str, str], state: dict[str, Any], *, limit: int) -> dict[str, Any]:
    """VK clips from channel screen names (e.g. pubgkotleta) and/or explicit URLs.

    Channel link is enough — no per-video links. Listing needs a **user** OAuth
    token (`VK_USER_ACCESS_TOKEN` / `PUBG_VK_ACCESS_TOKEN`) with `video` scope.
    Group tokens (VK_MLBB_ACCESS_TOKEN) cannot list another community's clips.
    """
    if os.environ.get("PUBG_VK_HARVEST", "1") != "1":
        return {"saved": 0, "skipped": "disabled"}

    raw = (env.get("PUBG_VK_VIDEO_URLS") or os.environ.get("PUBG_VK_VIDEO_URLS") or "").strip()
    urls = [u.strip() for u in raw.split(",") if u.strip().startswith("http")]
    urls.extend(VK_VIDEO_URLS)

    channels = _vk_channels(env)
    token = _vk_user_token(env)
    channel_errors: list[str] = []
    if channels and token:
        for screen in channels:
            oid = _vk_owner_id(screen, token=token)
            if oid is None:
                channel_errors.append(f"resolve_fail:{screen}")
                continue
            found = _vk_list_channel_urls(oid, token=token, limit=limit)
            if not found:
                channel_errors.append(f"empty_or_denied:{screen}:{oid}")
            urls.extend(found)
    elif channels and not token:
        return {
            "saved": 0,
            "skipped": "vk_needs_user_token",
            "channels": channels,
            "hint": (
                "Канал уже задан (pubgkotleta / клипы). Отдельные ссылки не нужны. "
                "Один раз положи user-токен VK со scope video в PUBG_VK_ACCESS_TOKEN "
                "(или VK_USER_ACCESS_TOKEN) — group-токен MLBB не подходит. "
                "Либо Netscape cookies в /root/vk_cookies.txt после логина."
            ),
            "attempted": 0,
        }

    # De-dupe.
    dedup: list[str] = []
    seen_u: set[str] = set()
    for u in urls:
        if u not in seen_u:
            seen_u.add(u)
            dedup.append(u)
    urls = dedup

    if not urls:
        return {
            "saved": 0,
            "skipped": "vk_no_urls",
            "channels": channels,
            "errors": channel_errors[:10],
            "hint": "Канал не отдал клипы — проверь user-токен (video) или задай PUBG_VK_VIDEO_URLS",
            "attempted": 0,
        }

    out_dir = Path(os.environ.get("PUBG_VK_DIR", "/root/datasets/vk/pubg"))
    out_dir.mkdir(parents=True, exist_ok=True)
    seen = set(state.get("seen_urls") or [])
    saved = 0
    attempted = 0
    errors: list[str] = list(channel_errors[:5])
    use_proxy = _use_proxy(env, prefer=False)  # VK usually reachable direct from VPS
    cookies = _vk_cookies()
    random.shuffle(urls)
    for url in urls:
        if saved >= limit:
            break
        if url in seen:
            continue
        attempted += 1
        vid = _id_from_url(url, "vk")
        dest = out_dir / f"{vid}.mp4"
        ok = _download(url, dest, env=env, use_proxy=use_proxy, cookies=cookies)
        seen.add(url)
        if ok:
            saved += 1
            state["downloaded"] = int(state.get("downloaded") or 0) + 1
            by = dict(state.get("by_source") or {})
            by["vk"] = int(by.get("vk") or 0) + 1
            state["by_source"] = by
        else:
            errors.append(f"vk_fail:{vid}")
        time.sleep(random.uniform(8, 18))
    state["seen_urls"] = list(seen)
    return {
        "saved": saved,
        "attempted": attempted,
        "errors": errors[:10],
        "out": str(out_dir),
        "channels": channels,
        "listed": len(urls),
    }

def run_harvest(*, per_source: int | None = None) -> dict[str, Any]:
    env = {**os.environ, **load_env(ENV_PATH)}
    state = load_state()
    per_source = per_source or _env_int("PUBG_MULTI_HARVEST_PER_SOURCE", 4)
    proxy = _proxy(env)
    report: dict[str, Any] = {
        "per_source": per_source,
        "proxy_configured": bool(proxy),
        "proxy_alive": _proxy_alive(proxy) if proxy else False,
        "ytdlp": ytdlp_bin(env),
        "sources": {},
    }

    if os.environ.get("PUBG_TIKTOK_HARVEST", "1") == "1":
        report["sources"]["tiktok"] = harvest_tiktok(env, state, limit=per_source)
        save_state(state)

    if os.environ.get("PUBG_INSTAGRAM_HARVEST", "1") == "1":
        report["sources"]["instagram"] = harvest_instagram(env, state, limit=per_source)
        save_state(state)

    if os.environ.get("PUBG_VK_HARVEST", "1") == "1":
        report["sources"]["vk"] = harvest_vk(env, state, limit=max(1, per_source // 2))
        save_state(state)

    report["downloaded_total"] = state.get("downloaded")
    report["by_source"] = state.get("by_source")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="PUBG multi-platform short harvest")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="Gentle batch from TikTok / Instagram / VK")
    run_p.add_argument("--per-source", type=int, default=0)
    sub.add_parser("status", help="Show harvest state")
    args = parser.parse_args()
    if args.cmd == "status":
        env = {**os.environ, **load_env(ENV_PATH)}
        proxy = _proxy(env)
        print(json.dumps(load_state(), ensure_ascii=False, indent=2))
        cookies = _cookies()
        print(
            json.dumps(
                {
                    "proxy_configured": bool(proxy),
                    "proxy_alive": _proxy_alive(proxy) if proxy else False,
                    "instagram_cookies": str(cookies) if cookies else None,
                    "vk_cookies": str(_vk_cookies()) if _vk_cookies() else None,
                    "vk_user_token": bool(_vk_user_token(env)),
                    "vk_channels": _vk_channels(env),
                    "ytdlp": ytdlp_bin(env),
                    "tiktok_users": _tiktok_users(env),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.cmd == "run":
        out = run_harvest(per_source=args.per_source or None)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
