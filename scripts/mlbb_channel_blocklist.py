#!/usr/bin/env python3
"""Blocked YouTube channels — staged skin reviews, non-gameplay content."""

from __future__ import annotations

import os
import re

# Staged skin-review channels — not usable for montage / highlight learning.
DEFAULT_BLOCKED_CHANNELS = (
    "@JessNoLimit",
    "JessNoLimit",
    "Jess No Limit",
)


def blocked_channel_tokens(env: dict[str, str] | None = None) -> tuple[str, ...]:
    env = env or dict(os.environ)
    tokens: list[str] = list(DEFAULT_BLOCKED_CHANNELS)
    extra = str(env.get("MLBB_BLOCKED_CHANNELS", "")).strip()
    if extra:
        tokens.extend(part.strip() for part in extra.split(",") if part.strip())
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        key = token.casefold()
        if key not in seen:
            seen.add(key)
            out.append(token)
    return tuple(out)


def _norm_channel(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


def matches_blocked_channel(text: str, env: dict[str, str] | None = None) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    norm_text = _norm_channel(raw)
    if not norm_text:
        return False
    for token in blocked_channel_tokens(env):
        norm_token = _norm_channel(token)
        if not norm_token:
            continue
        if norm_token in norm_text or norm_text in norm_token:
            return True
    return False


def is_blocked_feed_url(url: str, env: dict[str, str] | None = None) -> bool:
    return matches_blocked_channel(url, env)


def filter_channel_feeds(feeds: list[str], env: dict[str, str] | None = None) -> list[str]:
    return [url for url in feeds if not is_blocked_feed_url(url, env)]


def is_blocked_candidate(row: dict, env: dict[str, str] | None = None) -> tuple[bool, str]:
    for field in ("channel", "search_query", "url"):
        value = str(row.get(field) or "").strip()
        if value and matches_blocked_channel(value, env):
            return True, f"blocked_channel:{field}"
    return False, ""
