from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

from .montage_config import HeroMontageProfile
from .scene_analysis import is_excluded, probe_duration, text_matches_hero


@dataclass(slots=True)
class GameplayClip:
    path: Path
    score: float
    start_sec: float | None
    duration_sec: float | None
    hero_hint: str | None
    reason: str


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _first_column(row: dict[str, str], names: list[str]) -> str | None:
    for name in names:
        if name in row and str(row[name]).strip():
            return str(row[name]).strip()
    return None


def _float_or_none(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def resolve_video_path(raw_path: str, video_root: Path) -> Path | None:
    path = Path(raw_path)
    if path.is_file():
        return path.resolve()
    candidate = video_root / raw_path
    if candidate.is_file():
        return candidate.resolve()
    candidate = video_root / path.name
    if candidate.is_file():
        return candidate.resolve()
    for match in video_root.rglob(path.name):
        if match.is_file():
            return match.resolve()
    return None


def load_gameplay_clips(
    csv_path: Path,
    *,
    video_root: Path,
    hero: HeroMontageProfile,
    exclude_keywords: list[str],
) -> list[GameplayClip]:
    clips: list[GameplayClip] = []
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return clips

        for row in reader:
            normalized = {str(k).strip().lower(): (v or "") for k, v in row.items() if k}
            if not _truthy(_first_column(normalized, ["is_gameplay", "gameplay_only", "gameplay"])):
                continue

            if _truthy(_first_column(normalized, ["is_promo", "is_event", "is_cinematic", "is_skin", "is_ad"])):
                continue

            raw_path = _first_column(
                normalized,
                ["path", "file_path", "video_path", "filepath", "file", "source_path"],
            )
            if not raw_path:
                continue

            resolved = resolve_video_path(raw_path, video_root)
            if resolved is None:
                continue

            text_blob = " ".join(
                part
                for part in [
                    raw_path,
                    _first_column(normalized, ["description", "caption", "text", "title"]) or "",
                    _first_column(normalized, ["hero", "hero_name", "hero_guess", "label", "tags"]) or "",
                ]
                if part
            )
            if exclude_keywords and is_excluded(text_blob, exclude_keywords):
                continue

            hero_col = _first_column(normalized, ["hero", "hero_name", "hero_guess", "label"])
            if hero_col and not text_matches_hero(hero_col, hero.keywords):
                if not text_matches_hero(text_blob, hero.keywords):
                    continue
            elif not text_matches_hero(text_blob, hero.keywords):
                continue

            score = 0.0
            for col in ("gameplay_score", "quality_score", "score", "motion_score", "rank_score"):
                value = _float_or_none(normalized.get(col))
                if value is not None:
                    score = max(score, value)
            views = _float_or_none(normalized.get("view_count"))
            likes = _float_or_none(normalized.get("like_count"))
            if views is not None:
                score += math.log1p(views) * 2.0
            if likes is not None:
                score += math.log1p(likes)

            start_sec = _float_or_none(
                _first_column(
                    normalized,
                    [
                        "best_start_sec",
                        "segment_start_sec",
                        "clip_start_sec",
                        "start_sec",
                        "best_start",
                    ],
                )
            )
            duration_sec = _float_or_none(
                _first_column(
                    normalized,
                    [
                        "clip_duration_sec",
                        "segment_duration_sec",
                        "best_duration_sec",
                        "duration_sec",
                    ],
                )
            )

            clips.append(
                GameplayClip(
                    path=resolved,
                    score=score,
                    start_sec=start_sec,
                    duration_sec=duration_sec,
                    hero_hint=hero_col,
                    reason=f"score={score:.2f}",
                )
            )

    clips.sort(key=lambda item: item.score, reverse=True)
    return clips
