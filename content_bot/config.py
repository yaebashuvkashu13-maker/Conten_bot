from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class TelegramConfig:
    bot_token: str
    channel_id: str


@dataclass(slots=True)
class InstagramSource:
    name: str
    url: str
    max_entries: int = 5


@dataclass(slots=True)
class AppConfig:
    telegram: TelegramConfig
    instagram_sources: list[InstagramSource]
    state_path: Path
    instagram_cookies_path: Path | None = None
    proxy_url: str | None = None
    dry_run: bool = False


def _require(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise ValueError(f"Missing required config key: {key}")
    return mapping[key]


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text()) or {}

    telegram_raw = _require(raw, "telegram")
    sources_raw = _require(raw, "instagram_sources")

    telegram = TelegramConfig(
        bot_token=str(_require(telegram_raw, "bot_token")),
        channel_id=str(_require(telegram_raw, "channel_id")),
    )

    instagram_sources = [
        InstagramSource(
            name=str(source.get("name") or source.get("url")),
            url=str(_require(source, "url")),
            max_entries=int(source.get("max_entries", 5)),
        )
        for source in sources_raw
    ]

    state_path = Path(raw.get("state_path", ".content-bot-state.json"))
    cookies_raw = raw.get("instagram_cookies_path")
    instagram_cookies_path = Path(cookies_raw) if cookies_raw else None
    proxy_url = str(raw["proxy_url"]) if raw.get("proxy_url") else None
    dry_run = bool(raw.get("dry_run", False))

    return AppConfig(
        telegram=telegram,
        instagram_sources=instagram_sources,
        state_path=state_path,
        instagram_cookies_path=instagram_cookies_path,
        proxy_url=proxy_url,
        dry_run=dry_run,
    )

