from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .proxy_config import resolve_proxy_url


@dataclass(slots=True)
class TikTokProfile:
    url: str
    label: str
    max_entries: int = 50


@dataclass(slots=True)
class CollectConfig:
    proxy_url: str | None
    output_dir: Path
    download_media: bool
    delay_between_profiles_seconds: float
    tiktok_profiles: list[TikTokProfile]
    instagram_config_path: Path | None
    instagram_snapshot_dir: Path | None


def load_collect_config(path: str | Path) -> CollectConfig:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}

    proxy_url = resolve_proxy_url(str(raw["proxy_url"]) if raw.get("proxy_url") else None)
    output_dir = Path(raw.get("output_dir", "datasets/tiktok"))
    download_media = bool(raw.get("download_media", True))
    delay = float(raw.get("delay_between_profiles_seconds", 5.0))

    profiles: list[TikTokProfile] = []
    for item in raw.get("tiktok_profiles") or []:
        profiles.append(
            TikTokProfile(
                url=str(item["url"]),
                label=str(item.get("label") or item["url"]),
                max_entries=int(item.get("max_entries", 50)),
            )
        )

    instagram_config = raw.get("instagram_config_path")
    instagram_snapshot = raw.get("instagram_snapshot_dir")

    return CollectConfig(
        proxy_url=proxy_url,
        output_dir=output_dir,
        download_media=download_media,
        delay_between_profiles_seconds=delay,
        tiktok_profiles=profiles,
        instagram_config_path=Path(instagram_config) if instagram_config else None,
        instagram_snapshot_dir=Path(instagram_snapshot) if instagram_snapshot else None,
    )
