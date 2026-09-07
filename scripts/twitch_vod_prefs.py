#!/usr/bin/env python3
"""Twitch VOD discovery for PUBG Metro Royale (archives via yt-dlp, no Helix required)."""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

from youtube_shooter_vod_prefs import BAD_TITLE_RE, LIVE_TITLE_RE, title_ok as shooter_title_ok

# RU PUBG Mobile / Metro Royale (owner list). Override: TWITCH_PUBG_CHANNELS=login1,login2,...
DEFAULT_PUBG_CHANNELS: tuple[str, ...] = (
    "shifuwoe",
    "aderrtheman",
    "karat_pm",
    "b1_kitty",
    "zzzerbin",
    "tw_lexa",
    "tagav23",
    "amazonka_aa",
    "essko21",
    "lada2oo",
    "spulae111",
)

TWITCH_VOD_TITLE_RE = re.compile(
    r"pubg|playerunknown|battlegrounds|metro\s*royale|пабг|метро|mobile",
    re.I,
)
TWITCH_SKIP_TITLE_RE = re.compile(
    r"just\s*chatting|react|irl|music|giveaway|watch\s*party|"
    r"обзор|гайд|guide|tutorial|#short",
    re.I,
)


def twitch_vod_enabled(game: str = "pubg") -> bool:
    if os.environ.get("TWITCH_VOD_ENABLED", "0") != "1":
        return False
    return game.strip().lower() == "pubg"


def channel_logins(game: str = "pubg") -> list[str]:
    raw = os.environ.get("TWITCH_PUBG_CHANNELS", "").strip()
    if raw:
        return [c.strip().lower() for c in raw.split(",") if c.strip()]
    if game.strip().lower() == "pubg":
        return list(DEFAULT_PUBG_CHANNELS)
    return []


def _known_channel_logins(game: str = "pubg") -> set[str]:
    return {c.lower() for c in channel_logins(game)}


def title_ok(game: str, title: str, *, channel_login: str = "") -> bool:
    t = title or ""
    if LIVE_TITLE_RE.search(t) or BAD_TITLE_RE.search(t) or TWITCH_SKIP_TITLE_RE.search(t):
        return False
    if game.strip().lower() != "pubg":
        return False
    ch = channel_login.strip().lower()
    if ch and ch in _known_channel_logins(game):
        # Whitelisted Metro streamers — accept archives unless title is clearly off-topic.
        return True
    if TWITCH_VOD_TITLE_RE.search(t):
        return True
    return shooter_title_ok(game, t)


def twitch_video_url(video_id: str) -> str:
    return f"https://www.twitch.tv/videos/{video_id}"


def vod_discovery_search_cycle(
    cycle: int,
    game: str,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Rotate Twitch channel archive pages (yt-dlp flat-playlist)."""
    env = env or {}
    channels = channel_logins(game)
    batch = int(env.get("TWITCH_VOD_SEARCH_BATCH", env.get("SHOOTER_VOD_SEARCH_BATCH", "6")))
    delay = float(env.get("TWITCH_VOD_SEARCH_DELAY", env.get("SHOOTER_VOD_SEARCH_DELAY", "6")))
    limit = int(env.get("TWITCH_VOD_SEARCH_LIMIT", "12"))
    if not channels:
        return {"queries": [], "urls": [], "batch": batch, "delay": delay, "limit": limit}
    offset = (cycle * batch) % len(channels)
    picked = [channels[(offset + i) % len(channels)] for i in range(min(batch, len(channels)))]
    urls = [
        f"https://www.twitch.tv/{login}/videos?filter=archives&sort=time"
        for login in picked
    ]
    return {
        "queries": picked,
        "urls": urls,
        "batch": batch,
        "delay": delay,
        "limit": limit,
        "cycle": cycle,
        "game": game,
        "source": "twitch",
    }


def parse_flat_playlist_line(line: str) -> dict[str, str] | None:
    parts = line.split("|", 4)
    if len(parts) < 2:
        return None
    vid = parts[0].strip()
    if not vid.isdigit():
        return None
    title = parts[1]
    try:
        dur = float(parts[2]) if len(parts) > 2 and parts[2] not in ("NA", "None", "") else 0.0
    except ValueError:
        dur = 0.0
    uploader = parts[3][:60] if len(parts) > 3 else ""
    live_status = (parts[4] if len(parts) > 4 else "").strip().lower()
    return {
        "id": vid,
        "title": title[:120],
        "duration": str(dur),
        "uploader": uploader,
        "live_status": live_status,
        "url": twitch_video_url(vid),
        "source": "twitch",
    }
