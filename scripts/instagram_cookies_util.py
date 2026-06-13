#!/usr/bin/env python3
"""Normalize Cookie-Editor / Netscape exports for gallery-dl."""

from __future__ import annotations

import re
from pathlib import Path


def normalize_instagram_cookies_text(text: str) -> str:
    """Cookie-Editor prefixes domains with #HttpOnly_ — that comments out lines in Netscape parsers."""
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line.replace("#HttpOnly_", "", 1)
        elif re.match(r"^#\.instagram\.com\b", line):
            line = line[1:]
        out.append(line)
    body = "\n".join(out).strip() + "\n"
    if "instagram.com" not in body.lower():
        raise ValueError("нет cookies для instagram.com")
    if "sessionid" not in body.lower():
        raise ValueError("нет sessionid — экспортируйте cookies будучи залогинены в Instagram")
    return body


def normalize_instagram_cookies_file(path: Path) -> Path:
    raw = path.read_text(encoding="utf-8", errors="replace")
    fixed = normalize_instagram_cookies_text(raw)
    if fixed != raw:
        path.write_text(fixed, encoding="utf-8")
    return path
