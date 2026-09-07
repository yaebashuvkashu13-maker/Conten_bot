#!/usr/bin/env python3
"""Multi-platform short-video harvest for PUBG Metro silver learning.

Sources (gentle, rate-limited):
  - TikTok search (requires YTDLP_PROXY / SOCKS5_PROXY)
  - Instagram Reels / tags (requires INSTAGRAM_COOKIES_PATH)
  - VK clips (optional, yt-dlp)

Downloaded files land under /root/datasets/{tiktok,instagram,vk}/pubg/
and are scored by pubg_shorts_autolearn local pool (no extra YouTube pressure).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

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

TIKTOK_QUERIES = (
    "метро роял пабг",
    "metro royale pubg",
    "pubg metro royale clutch",
    "метро роял перестрелка",
    "pubg mobile metro royale",
)

INSTAGRAM_TAGS = (
    "metrroyale",
    "metroroyale",
    "pubgmetrroyale",
    "pubgmobilemetro",
)

VK_QUERIES = (
    "метро роял пабг",
    "metro royale pubg",
)


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
        return {"seen_urls": [], "downloaded": 0, "by_source": {}}
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
    proxy = _proxy(env) if use_proxy else ""
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


def _search_urls(
    extractor_query: str,
    *,
    env: dict[str, str],
    use_proxy: bool,
    cookies: Path | None = None,
    limit: int = 15,
) -> list[str]:
    cmd = _ytdlp_base(env, use_proxy=use_proxy, cookies=cookies) + [
        "--flat-playlist",
        "--print",
        "%(webpage_url)s",
        extractor_query,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=150)
    except subprocess.TimeoutExpired:
        return []
    urls: list[str] = []
    for line in (proc.stdout or "").splitlines():
        url = line.strip()
        if not url.startswith("http"):
            continue
        urls.append(url)
        if len(urls) >= limit:
            break
    return urls


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
    # Resolve whatever extension landed.
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


def harvest_tiktok(env: dict[str, str], state: dict[str, Any], *, limit: int) -> dict[str, Any]:
    proxy = _proxy(env)
    if not proxy:
        return {"saved": 0, "skipped": "no_proxy", "attempted": 0}
    out_dir = Path(os.environ.get("PUBG_TIKTOK_DIR", "/root/datasets/tiktok/pubg"))
    out_dir.mkdir(parents=True, exist_ok=True)
    seen = set(state.get("seen_urls") or [])
    saved = 0
    attempted = 0
    errors: list[str] = []
    queries = list(TIKTOK_QUERIES)
    random.shuffle(queries)
    for query in queries:
        if saved >= limit:
            break
        urls = _search_urls(f"tiktoksearch{max(12, limit * 2)}:{query}", env=env, use_proxy=True, limit=12)
        random.shuffle(urls)
        time.sleep(random.uniform(8, 18))
        for url in urls:
            if saved >= limit:
                break
            if url in seen:
                continue
            attempted += 1
            vid = _id_from_url(url, "tt")
            dest = out_dir / f"{vid}.mp4"
            ok = _download(url, dest, env=env, use_proxy=True)
            seen.add(url)
            if ok:
                saved += 1
                state["downloaded"] = int(state.get("downloaded") or 0) + 1
                by = dict(state.get("by_source") or {})
                by["tiktok"] = int(by.get("tiktok") or 0) + 1
                state["by_source"] = by
            else:
                errors.append(f"tt_fail:{vid}")
            time.sleep(random.uniform(14, 32))
    state["seen_urls"] = list(seen)
    return {"saved": saved, "attempted": attempted, "errors": errors[:10], "out": str(out_dir)}


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
    # Prefer proxy for IG too when available (datacenter IP often blocked).
    use_proxy = bool(_proxy(env))
    for tag in tags:
        if saved >= limit:
            break
        # yt-dlp Instagram tag / reel search is fragile; try tag URL first.
        query = f"https://www.instagram.com/explore/tags/{tag}/"
        urls = _search_urls(query, env=env, use_proxy=use_proxy, cookies=cookies, limit=10)
        if not urls:
            # Fallback: igsearch if extractor exists in this yt-dlp build.
            urls = _search_urls(
                f"igsearch{max(8, limit)}:{tag} metro royale",
                env=env,
                use_proxy=use_proxy,
                cookies=cookies,
                limit=8,
            )
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


def harvest_vk(env: dict[str, str], state: dict[str, Any], *, limit: int) -> dict[str, Any]:
    """VK short clips via yt-dlp search — optional third source."""
    if os.environ.get("PUBG_VK_HARVEST", "1") != "1":
        return {"saved": 0, "skipped": "disabled"}
    out_dir = Path(os.environ.get("PUBG_VK_DIR", "/root/datasets/vk/pubg"))
    out_dir.mkdir(parents=True, exist_ok=True)
    seen = set(state.get("seen_urls") or [])
    saved = 0
    attempted = 0
    errors: list[str] = []
    use_proxy = bool(_proxy(env))
    queries = list(VK_QUERIES)
    random.shuffle(queries)
    for query in queries:
        if saved >= limit:
            break
        urls = _search_urls(
            f"vksearch{max(10, limit * 2)}:{query}",
            env=env,
            use_proxy=use_proxy,
            limit=10,
        )
        random.shuffle(urls)
        time.sleep(random.uniform(6, 14))
        for url in urls:
            if saved >= limit:
                break
            if url in seen:
                continue
            attempted += 1
            vid = _id_from_url(url, "vk")
            dest = out_dir / f"{vid}.mp4"
            ok = _download(url, dest, env=env, use_proxy=use_proxy)
            seen.add(url)
            if ok:
                saved += 1
                state["downloaded"] = int(state.get("downloaded") or 0) + 1
                by = dict(state.get("by_source") or {})
                by["vk"] = int(by.get("vk") or 0) + 1
                state["by_source"] = by
            else:
                errors.append(f"vk_fail:{vid}")
            time.sleep(random.uniform(10, 24))
    state["seen_urls"] = list(seen)
    return {"saved": saved, "attempted": attempted, "errors": errors[:10], "out": str(out_dir)}


def run_harvest(*, per_source: int | None = None) -> dict[str, Any]:
    env = {**os.environ, **load_env(ENV_PATH)}
    state = load_state()
    per_source = per_source or _env_int("PUBG_MULTI_HARVEST_PER_SOURCE", 4)
    report: dict[str, Any] = {"per_source": per_source, "sources": {}}

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
        print(json.dumps(load_state(), ensure_ascii=False, indent=2))
        cookies = _cookies()
        print(
            json.dumps(
                {
                    "proxy_configured": bool(_proxy({**os.environ, **load_env(ENV_PATH)})),
                    "instagram_cookies": str(cookies) if cookies else None,
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
