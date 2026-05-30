from __future__ import annotations

import argparse
import json
from pathlib import Path

from .ffmpeg_montage import assemble_montage
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


def pick_scenes(
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


def _send_to_telegram(output_path: Path, caption: str, telegram_config_path: str) -> dict:
    from .config import load_config
    from .telegram_publisher import TelegramPublisher

    app_config = load_config(telegram_config_path)
    publisher = TelegramPublisher(app_config.telegram)
    return publisher.send_video_file(output_path, caption=caption)


def build_montage(
    hero: str,
    config: MontageConfig,
    *,
    dry_run: bool = False,
    output_name: str | None = None,
    send_telegram: bool = False,
    telegram_config_path: str = "config.yaml",
) -> dict:
    hero_key = hero.lower()
    profile = config.heroes.get(hero_key)
    if profile is None:
        raise ValueError(f"Unknown hero '{hero}'. Known: {', '.join(sorted(config.heroes))}")

    if not config.video_root.exists():
        raise FileNotFoundError(
            f"Video root not found: {config.video_root}. "
            "Expected your TikTok MLBB library at datasets/tiktok/mlbb."
        )

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
            f"found {len(candidates)}, need {config.montage.scene_count}. "
            "Check manifest keywords or hero labels in filenames/descriptions."
        )

    scene_duration = _target_scene_duration(config.montage)
    scenes = pick_scenes(
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

    output_name = output_name or f"{hero_key}_{len(scenes)}scenes.mp4"
    output_path = config.output_dir / hero_key / output_name
    plan = {
        "hero": profile.name,
        "hook": profile.hook,
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
    )
    plan["rendered"] = True

    if send_telegram:
        caption = f"{profile.hook}\n{profile.name} montage · {round(total_duration)}s"
        tg_result = _send_to_telegram(output_path, caption, telegram_config_path)
        plan["telegram"] = {"ok": tg_result.get("ok"), "chat_id": tg_result.get("result", {}).get("chat", {}).get("id")}
    return plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a 33-57s gameplay montage (3-4 scenes) for one MLBB hero."
    )
    parser.add_argument("--hero", required=True, help="Hero key: gusion, lancelot, chou, fanny, hayabusa")
    parser.add_argument("--config", default="config.montage.yaml", help="Montage YAML config.")
    parser.add_argument(
        "--video-root",
        help="Override video library path (default datasets/tiktok/mlbb).",
    )
    parser.add_argument("--output-name", help="Output filename.")
    parser.add_argument("--dry-run", action="store_true", help="Print scene plan without rendering.")
    parser.add_argument("--scene-count", type=int, help="Override number of scenes (3 or 4).")
    parser.add_argument(
        "--send-telegram",
        action="store_true",
        help="Upload rendered montage to Telegram (requires config.yaml).",
    )
    parser.add_argument(
        "--telegram-config",
        default="config.yaml",
        help="YAML with telegram.bot_token and telegram.channel_id (your chat id).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config_path = Path(args.config)
    config = load_montage_config(config_path) if config_path.exists() else default_config()
    if args.video_root:
        config.video_root = Path(args.video_root)
    if args.scene_count:
        config.montage.scene_count = args.scene_count

    plan = build_montage(
        args.hero,
        config,
        dry_run=args.dry_run,
        output_name=args.output_name,
        send_telegram=args.send_telegram,
        telegram_config_path=args.telegram_config,
    )
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
