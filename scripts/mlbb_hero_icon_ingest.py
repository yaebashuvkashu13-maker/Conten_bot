#!/usr/bin/env python3
"""Download MLBB hero portrait icons for own-kill banner matching."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAPI_LIST = "https://mapi.mobilelegends.com/hero/list"
FANDOM_API = "https://mobile-legends.fandom.com/api.php"
UA = "content-bot-mlbb-hero-icons/1.0"


def repo_root() -> Path:
    env = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent.parent
    return root if (root / "config").exists() else Path("/root/content_bot_ml")


def icon_root() -> Path:
    env = os.environ.get("MLBB_HERO_ICON_ROOT", "").strip()
    if env:
        return Path(env)
    return repo_root() / "data" / "mlbb_hero_icons"


def _heroes_json() -> Path:
    env = os.environ.get("MLBB_HEROES_JSON", "").strip()
    if env:
        return Path(env)
    return repo_root() / "config" / "mlbb_heroes.json"


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _download(url: str, dest: Path, *, timeout: float = 45.0) -> bool:
    if url.startswith("//"):
        url = "https:" + url
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
    if len(data) < 400:
        return False
    dest.write_bytes(data)
    return True


def _fetch_mapi_map() -> dict[str, str]:
    req = urllib.request.Request(MAPI_LIST, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    out: dict[str, str] = {}
    for row in payload.get("data") or []:
        name = str(row.get("name") or "")
        key = str(row.get("key") or "")
        if name and key:
            out[_norm_name(name)] = key
    return out


def _fandom_icon_url(hero_name: str) -> str | None:
    """Fallback: File:{Hero}.png from Fandom wiki."""
    title = f"File:{hero_name}.png"
    qs = urllib.parse.urlencode(
        {
            "action": "query",
            "prop": "imageinfo",
            "iiprop": "url",
            "format": "json",
            "titles": title,
        }
    )
    req = urllib.request.Request(f"{FANDOM_API}?{qs}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    pages = (data.get("query") or {}).get("pages") or {}
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        url = str(info.get("url") or "")
        if url.startswith("http"):
            return url
    return None


def _config_heroes() -> list[dict]:
    path = _heroes_json()
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("heroes") or [])


def download_hero_icons(*, all_mapi: bool = False, force: bool = False) -> dict:
    root = icon_root()
    root.mkdir(parents=True, exist_ok=True)
    mapi = _fetch_mapi_map()
    rows: list[tuple[str, str]] = []
    if all_mapi:
        # Map every official hero name -> slug id.
        for norm, _url in sorted(mapi.items()):
            rows.append((norm, norm))
    else:
        for hero in _config_heroes():
            hid = str(hero.get("id") or "")
            tags = [str(hero.get("id") or ""), *(hero.get("tags") or [])]
            display = tags[-1] if tags else hid
            rows.append((hid, display))

    ok = skip = fail = 0
    manifest: list[dict] = []
    for hid, display in rows:
        dest = root / hid / "icon.png"
        if dest.exists() and dest.stat().st_size > 400 and not force:
            skip += 1
            manifest.append({"id": hid, "path": str(dest.relative_to(root)), "source": "cached"})
            continue
        norm = _norm_name(display.replace("_", " "))
        url = mapi.get(norm) or mapi.get(_norm_name(hid))
        source = "mapi"
        if not url:
            fandom_name = display.replace("_", " ").title()
            url = _fandom_icon_url(fandom_name)
            source = "fandom"
        if not url:
            fail += 1
            continue
        if _download(url, dest):
            ok += 1
            manifest.append({"id": hid, "path": str(dest.relative_to(root)), "source": source, "url": url})
        else:
            fail += 1
        time.sleep(0.12)

    meta = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "root": str(root),
        "downloaded": ok,
        "skipped": skip,
        "failed": fail,
        "heroes": manifest,
    }
    (root / "manifest.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description="Download MLBB hero icons for banner matching")
    parser.add_argument("--all-mapi", action="store_true", help="Download all 120+ official heroes")
    parser.add_argument("--force", action="store_true", help="Re-download even if cached")
    args = parser.parse_args()
    meta = download_hero_icons(all_mapi=args.all_mapi, force=args.force)
    print(
        f"hero icons: ok={meta['downloaded']} skip={meta['skipped']} fail={meta['failed']} "
        f"root={meta['root']}"
    )
    return 0 if meta["failed"] == 0 or meta["downloaded"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
