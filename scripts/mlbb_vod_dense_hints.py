#!/usr/bin/env python3
"""Load known banner seconds from dense-audit JSON for faster savage discovery."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path


def _audit_path() -> Path:
    return Path(
        os.environ.get(
            "MLBB_DENSE_AUDIT_JSON",
            "/root/data/mlbb/dense_audit_2026-07-08.json",
        )
    )


@lru_cache(maxsize=1)
def _audit_index() -> dict[str, list[dict]]:
    path = _audit_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, list[dict]] = {}
    for block in data.get("vods", []):
        vid = str(block.get("vod", "")).replace("yt_", "").replace(".mp4", "").strip()
        if not vid:
            continue
        hits = block.get("banner_times") or []
        if hits:
            out[vid.lower()] = list(hits)
    return out


def audit_banner_hints(vod_id: str, *, min_tier: int = 0) -> list[float]:
    """Return banner peak seconds from last dense audit for this youtube id."""
    vid = str(vod_id or "").replace("yt_", "").replace(".mp4", "").strip().lower()
    if not vid:
        return []
    hits = _audit_index().get(vid, [])
    secs: list[float] = []
    for hit in hits:
        tier = int(hit.get("tier") or 0)
        if min_tier > 0 and tier < min_tier:
            continue
        try:
            secs.append(float(hit["sec"]))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(set(secs))
