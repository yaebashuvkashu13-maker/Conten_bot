#!/usr/bin/env python3
"""Title / file gates for PUBG YouTube Shorts calibration."""

from __future__ import annotations

import os
import re
from pathlib import Path

PUBG_TITLE_RE = re.compile(
    r"pubg|playerunknown|battlegrounds|metro[\s_-]*royale|пабг|метро",
    re.I,
)
OTHER_GAME_RE = re.compile(
    r"mobile\s*legends|\bmlbb\b|standoff|free\s*fire|codm|call\s*of\s*duty|fortnite|"
    r"genshin|minecraft|roblox",
    re.I,
)
BAD_TITLE_RE = re.compile(
    r"giveaway|#ad\b|sponsored|free\s+diamond|tutorial|guide|reaction|meme|"
    r"aim\s*trainer|training\s*mode",
    re.I,
)


def pubg_short_title_ok(title: str, *, query: str = "") -> bool:
    t = title or ""
    if OTHER_GAME_RE.search(t) or BAD_TITLE_RE.search(t):
        return False
    blob = f"{t} {query}"
    return bool(PUBG_TITLE_RE.search(blob))


def pubg_short_passes_calibration(path: Path, *, title: str = "") -> tuple[bool, float, str]:
    """Lenient gate — queue PUBG-looking Shorts for owner 👍/👎."""
    if not path.exists() or path.stat().st_size < 8000:
        return False, 0.0, "missing_file"
    if title and not pubg_short_title_ok(title):
        return False, 0.0, "not_pubg_title"
    metro_hint = "unknown"
    try:
        import subprocess

        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        dur = float((proc.stdout or "0").strip() or 0)
        if dur < 2.5 or dur > 65:
            return False, 0.0, "bad_duration"
        if os.environ.get("PUBG_SHORTS_METRO_TAG", "1") == "1":
            from pubg_metro_royale_gate import segment_looks_metro_royale

            ok_m, reason = segment_looks_metro_royale(path, max(0.5, dur * 0.2), min(12.0, dur * 0.8))
            metro_hint = "metro" if ok_m else f"non_metro:{reason[:40]}"
    except Exception as exc:
        metro_hint = f"tag_skip:{exc}"[:40]

    score = 0.62 if metro_hint == "metro" else 0.48
    return True, score, metro_hint
