from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .ffmpeg_montage import assemble_montage
from .gameplay_filter import GameplayClip, load_gameplay_clips
from .montage_config import MontageConfig, default_config, load_montage_config
from .scene_analysis import (
    SceneSegment,
    filter_candidates,
    find_best_segment,
    load_manifest_index,
    probe_duration,
)


def _target_scene_duration(settings) -> float:
    target_total = (settings.min_total_duration + settings.max_total_duration) / 2.0
    transitions = (settings.scene_count - 1) * settings.transition_duration
    raw = (target_total + transitions) / settings.scene_count
    return max(settings.min_scene_duration, min(settings.max_scene_duration, raw))


def pick_scenes_from_motion(
    candidates,
    *,
    scene_count: int,
    scene_duration: float,
) -> list[SceneSegment]:
    chosen: list[SceneSegment] = []
    used_paths: set[Path] = set()

    for candidate in candidates:
        if len(chosen) >= scene_count:
            break
        if candidate.path in used_paths:
            continue

        segment = find_best_segment(candidate.path, window_sec=scene_duration)
        if segment is None:
            continue
        segment.source_score = candidate.score
        segment.duration_sec = min(
            segment.duration_sec,
            scene_duration,
            max(probe_duration(candidate.path) - segment.start_sec, 1.0),
        )
        chosen.append(segment)
        used_paths.add(candidate.path)

    return chosen


def _clip_to_segment(clip: GameplayClip, scene_duration: float) -> SceneSegment | None:
    source_duration = probe_duration(clip.path)
    if source_duration <= 0:
        return None

    if clip.start_sec is not None and clip.duration_sec is not None:
        start = max(clip.start_sec, 0.0)
        duration = min(clip.duration_sec, scene_duration, max(source_duration - start, 1.0))
        return SceneSegment(clip.path, start, duration, clip.score, clip.score)

    segment = find_best_segment(clip.path, window_sec=scene_duration)
    if segment is None:
        return None
    segment.source_score = clip.score
    segment.duration_sec = min(
        segment.duration_sec,
        scene_duration,
        max(source_duration - segment.start_sec, 1.0),
    )
    return segment


def pick_scenes_from_gameplay(
    clips: list[GameplayClip],
    *,
    scene_count: int,
    scene_duration: float,
) -> list[SceneSegment]:
    chosen: list[SceneSegment] = []
    used_paths: set[Path] = set()

    for clip in clips:
        if len(chosen) >= scene_count:
            break
        if clip.path in used_paths:
            continue
        segment = _clip_to_segment(clip, scene_duration)
        if segment is None:
            continue
        chosen.append(segment)
        used_paths.add(clip.path)

    return chosen


def _send_to_telegram(output_path: Path, caption: str, telegram_config_path: str) -> dict:
    from .config import TelegramConfig, load_config
    from .telegram_publisher import TelegramPublisher

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        telegram = TelegramConfig(bot_token=token, channel_id=chat_id)
    else:
        telegram = load_config(telegram_config_path).telegram
    publisher = TelegramPublisher(telegram)
    return publisher.send_video_file(output_path, caption=caption)


def build_montage(
    hero: str,
    config: MontageConfig,
    *,
    dry_run: bool = False,
    output_name: str | None = None,
    send_telegram: bool = False,
    telegram_config_path: str = "config.yaml",
    gameplay_csv: Path | None = None,
) -> dict:
    hero_key = hero.lower()
    profile = config.heroes.get(hero_key)
    if profile is None:
        raise ValueError(f"Unknown hero '{hero}'. Known: {', '.join(sorted(config.heroes))}")

    if not config.video_root.exists():
        raise FileNotFoundError(
            f"Video root not found: {config.video_root}. "
            "If you expected ~1922 videos, this agent is not on the VM where they were downloaded."
        )

    csv_path = gameplay_csv or config.gameplay_csv
    scene_duration = _target_scene_duration(config.montage)
    source_mode = "manifest"

    if csv_path and csv_path.is_file():
        source_mode = "gameplay_csv"
        clips = load_gameplay_clips(
            csv_path,
            video_root=config.video_root,
            hero=profile,
            exclude_keywords=config.exclude_keywords,
        )
        if len(clips) < config.montage.scene_count:
            raise RuntimeError(
                f"Not enough gameplay-only clips for {profile.name} in {csv_path}: "
                f"found {len(clips)}, need {config.montage.scene_count}."
            )
        scenes = pick_scenes_from_gameplay(
            clips,
            scene_count=config.montage.scene_count,
            scene_duration=scene_duration,
        )
    else:
        manifest_index = load_manifest_index(config.manifest_glob)
        candidates = filter_candidates(
            config.video_root,
            manifest_index,
            hero_keywords=profile.keywords,
            exclude_keywords=config.exclude_keywords,
            sample_limit=config.montage.sample_candidates,
            min_source_duration=config.montage.min_source_duration,
        )
        if len(candidates) < config.montage.scene_count:
            raise RuntimeError(
                f"Not enough candidate videos for {profile.name}: "
                f"found {len(candidates)}, need {config.montage.scene_count}."
            )
        scenes = pick_scenes_from_motion(
            candidates,
            scene_count=config.montage.scene_count,
            scene_duration=scene_duration,
        )

    if len(scenes) < config.montage.scene_count:
        raise RuntimeError(
            f"Could only build {len(scenes)} gameplay scenes for {profile.name} "
            f"(target {config.montage.scene_count})."
        )

    transition = config.montage.transition_duration
    total_duration = sum(scene.duration_sec for scene in scenes) - transition * (len(scenes) - 1)

    output_name = output_name or f"{hero_key}_gameplay_{len(scenes)}scenes_smooth.mp4"
    output_path = config.output_dir / hero_key / output_name
    plan = {
        "hero": profile.name,
        "hook": profile.hook,
        "source_mode": source_mode,
        "gameplay_csv": str(csv_path) if csv_path else None,
        "output": str(output_path),
        "scene_duration_target": scene_duration,
        "estimated_total_duration": round(total_duration, 2),
        "scenes": [
            {
                "path": str(scene.path),
                "start_sec": round(scene.start_sec, 2),
                "duration_sec": round(scene.duration_sec, 2),
                "motion_score": round(scene.score, 4),
                "source_score": round(scene.source_score, 4),
            }
            for scene in scenes
        ],
    }

    if dry_run:
        return plan

    assemble_montage(
        scenes,
        output_path,
        transition_duration=transition,
        hook_text=profile.hook or None,
        clip_fade_sec=config.clip_fade_sec,
    )
    plan["rendered"] = True

    if send_telegram and os.environ.get("TELEGRAM_BOT_TOKEN"):
        caption = f"{profile.hook}\n{profile.name} montage · {round(total_duration)}s"
        tg_result = _send_to_telegram(output_path, caption, telegram_config_path)
        plan["telegram"] = {
            "ok": tg_result.get("ok"),
            "chat_id": tg_result.get("result", {}).get("chat", {}).get("id"),
        }
    elif send_telegram:
        plan["telegram"] = {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}

    return plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a 33-57s gameplay montage (3-4 scenes) for one MLBB hero."
    )
    parser.add_argument("--hero", required=True, help="Hero key: gusion, lancelot, chou, fanny, hayabusa")
    parser.add_argument("--config", default="config.montage.yaml", help="Montage YAML config.")
    parser.add_argument("--video-root", help="Override video library path.")
    parser.add_argument(
        "--gameplay-csv",
        default="datasets/tiktok/reports/gameplay_filter_full.csv",
        help="Gameplay-only filter CSV (is_gameplay=True).",
    )
    parser.add_argument("--output-name", help="Output filename.")
    parser.add_argument("--dry-run", action="store_true", help="Print scene plan without rendering.")
    parser.add_argument("--scene-count", type=int, help="Override number of scenes (3 or 4).")
    parser.add_argument("--send-telegram", action="store_true", help="Send result if TELEGRAM_BOT_TOKEN is set.")
    parser.add_argument("--telegram-config", default="config.yaml")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config_path = Path(args.config)
    config = load_montage_config(config_path) if config_path.exists() else default_config()
    if args.video_root:
        config.video_root = Path(args.video_root)
    if args.scene_count:
        config.montage.scene_count = args.scene_count

    gameplay_csv = Path(args.gameplay_csv) if args.gameplay_csv else None
    if gameplay_csv and not gameplay_csv.is_file():
        print(f"WARNING: gameplay CSV not found: {gameplay_csv} — falling back to manifest keywords.")

    plan = build_montage(
        args.hero,
        config,
        dry_run=args.dry_run,
        output_name=args.output_name,
        send_telegram=args.send_telegram,
        telegram_config_path=args.telegram_config,
        gameplay_csv=gameplay_csv if gameplay_csv and gameplay_csv.is_file() else None,
    )
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
