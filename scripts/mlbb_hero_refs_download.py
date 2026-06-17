#!/usr/bin/env python3
"""Download top-N MLBB hero reference images for showcase/skin-reveal gate."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# Meta-popular heroes (moderate pack — icon + optional splash per hero).
TOP_HERO_IDS = [
    84,  # Ling
    17,  # Fanny
    56,  # Gusion
    21,  # Hayabusa
    47,  # Lancelot
    103,  # Paquito
    97,  # Benedetta
    89,  # Wanwan
    105,  # Beatrix
    114,  # Melissa
    113,  # Yin
    31,  # Moskov
    40,  # Karrie
    65,  # Claude
    53,  # Lesley
    110,  # Valentina
    52,  # Pharsa
    115,  # Xavier
    91,  # Cecilion
    25,  # Kagura
    81,  # Esmeralda
    82,  # Terizla
    78,  # Khufra
    6,  # Tigreal
    10,  # Franco
    102,  # Mathilda
    55,  # Angela
    86,  # Lylia
    68,  # Lunox
    64,  # Aldous
]

HEROES_API = os.environ.get(
    "MLBB_HEROES_API",
    "https://unofficial-mobile-legends-api.vercel.app/api/heroes",
)
ROOT = Path(os.environ.get("MLBB_HERO_REFS_ROOT", "/root/datasets/mlbb/hero_refs"))
INDEX_PATH = Path(os.environ.get("MLBB_HERO_REFS_INDEX", str(ROOT / "index.json")))


def _fetch_json(url: str, *, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "mlbb-hero-refs/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _download(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 1024:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    if url.startswith("//"):
        url = "https:" + url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mlbb-hero-refs/1.0"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = resp.read()
        if len(data) < 512:
            return False
        dest.write_bytes(data)
        return True
    except Exception:
        return False


def _hero_map() -> dict[str, dict]:
    payload = _fetch_json(HEROES_API)
    rows = payload.get("data") or []
    out: dict[str, dict] = {}
    for row in rows:
        hid = str(row.get("heroid") or "").strip()
        if hid:
            out[hid] = row
    return out


def download_hero_refs(*, limit: int | None = None) -> dict:
    heroes = _hero_map()
    ids = TOP_HERO_IDS[: limit or len(TOP_HERO_IDS)]
    saved = skipped = 0
    index_rows: list[dict] = []

    for hid in ids:
        row = heroes.get(str(hid))
        if not row:
            skipped += 1
            continue
        name = str(row.get("name") or hid)
        hero_dir = ROOT / str(hid)
        icon_url = str(row.get("key") or "")
        icon_path = hero_dir / "icon.png"
        ok = _download(icon_url, icon_path) if icon_url else False
        paths = []
        if ok:
            saved += 1
            paths.append(str(icon_path))
        # Light augmentation: store a second copy as splash alias for histogram diversity.
        splash_path = hero_dir / "splash.png"
        if ok and _download(icon_url, splash_path):
            paths.append(str(splash_path))
        index_rows.append(
            {
                "hero_id": hid,
                "name": name,
                "paths": paths,
                "icon_url": icon_url,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        json.dumps({"heroes": index_rows, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2),
        encoding="utf-8",
    )
    return {"saved": saved, "skipped": skipped, "heroes": len(index_rows), "root": str(ROOT)}


def main() -> int:
    limit = None
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        if idx + 1 < len(sys.argv):
            limit = int(sys.argv[idx + 1])
    stats = download_hero_refs(limit=limit)
    print(json.dumps(stats, ensure_ascii=False), flush=True)
    return 0 if stats.get("heroes", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
