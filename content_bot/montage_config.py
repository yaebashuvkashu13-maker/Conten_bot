from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class HeroMontageProfile:
    name: str
    keywords: list[str]
    hook: str = ""


@dataclass(slots=True)
class MontageSettings:
    min_total_duration: float = 33.0
    max_total_duration: float = 57.0
    scene_count: int = 4
    transition_duration: float = 0.4
    min_scene_duration: float = 8.0
    max_scene_duration: float = 16.0
    sample_candidates: int = 40
    min_source_duration: float = 12.0


@dataclass(slots=True)
class MontageConfig:
    video_root: Path
    manifest_glob: str
    output_dir: Path
    montage: MontageSettings
    exclude_keywords: list[str]
    heroes: dict[str, HeroMontageProfile]


def _hero_profiles(raw: dict[str, Any]) -> dict[str, HeroMontageProfile]:
    heroes: dict[str, HeroMontageProfile] = {}
    for name, item in (raw or {}).items():
        keywords = [str(k).lower() for k in (item.get("keywords") or [name])]
        heroes[name.lower()] = HeroMontageProfile(
            name=name,
            keywords=keywords,
            hook=str(item.get("hook") or f"{name.upper()} MONTAGE"),
        )
    return heroes


def load_montage_config(path: str | Path) -> MontageConfig:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}
    montage_raw = raw.get("montage") or {}

    montage = MontageSettings(
        min_total_duration=float(montage_raw.get("min_total_duration", 33)),
        max_total_duration=float(montage_raw.get("max_total_duration", 57)),
        scene_count=int(montage_raw.get("scene_count", 4)),
        transition_duration=float(montage_raw.get("transition_duration", 0.4)),
        min_scene_duration=float(montage_raw.get("min_scene_duration", 8)),
        max_scene_duration=float(montage_raw.get("max_scene_duration", 16)),
        sample_candidates=int(montage_raw.get("sample_candidates", 40)),
        min_source_duration=float(montage_raw.get("min_source_duration", 12)),
    )

    return MontageConfig(
        video_root=Path(raw.get("video_root", "datasets/tiktok/mlbb")),
        manifest_glob=str(raw.get("manifest_glob", "datasets/tiktok/*_manifest.jsonl")),
        output_dir=Path(raw.get("output_dir", "output/montage")),
        montage=montage,
        exclude_keywords=[str(k).lower() for k in raw.get("exclude_keywords") or []],
        heroes=_hero_profiles(raw.get("heroes")),
    )


DEFAULT_HEROES: dict[str, HeroMontageProfile] = {
    "gusion": HeroMontageProfile("Gusion", ["gusion", "гусион"], "GUSION OUTPLAY"),
    "lancelot": HeroMontageProfile("Lancelot", ["lancelot", "ланселот"], "LANCELOT COMBO"),
    "chou": HeroMontageProfile("Chou", ["chou", "чоу", "kung fu"], "CHOU OUTPLAY"),
    "fanny": HeroMontageProfile("Fanny", ["fanny", "фанни"], "FANNY MECHANICS"),
    "hayabusa": HeroMontageProfile("Hayabusa", ["hayabusa", "хаябуса", "haya"], "HAYABUSA KILL"),
}


def default_config() -> MontageConfig:
    return MontageConfig(
        video_root=Path("datasets/tiktok/mlbb"),
        manifest_glob="datasets/tiktok/*_manifest.jsonl",
        output_dir=Path("output/montage"),
        montage=MontageSettings(),
        exclude_keywords=["giveaway", "event", "promo", "collab", "redeem", "skin release"],
        heroes=DEFAULT_HEROES,
    )
