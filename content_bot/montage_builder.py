from __future__ import annotations

import argparse
import csv
import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


DEFAULT_BANNED_WORDS = (
    "allstar",
    "animation",
    "cinematic",
    "collab",
    "collaboration",
    "collector",
    "diamonds",
    "draw",
    "event",
    "free skin",
    "gold accessory",
    "naruto",
    "official",
    "prize pool",
    "promo",
    "reward",
    "rewards",
    "skin",
    "skins",
    "starlight",
    "teaser",
    "trailer",
)

DEFAULT_BLOCKED_SOURCES = {
    "mlbb-official",
    "mobilelegends-id",
    "mobile-legends-game",
    "mlbb-esports",
    "mobilelegendsph",
    "mlbbphilippines",
}


@dataclass(slots=True)
class ClipCandidate:
    path: Path
    hero: str
    source_label: str
    description: str
    gameplay_score: float
    popularity_score: int
    duration: float


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _probe_duration(path: Path) -> float:
    output = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        timeout=20,
    )
    data = json.loads(output)
    return float(data["format"]["duration"])


def _has_audio(path: Path) -> bool:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    return bool(result.stdout.strip())


def _popularity_score(row: dict[str, str]) -> int:
    views = int(float(row.get("view_count") or 0))
    likes = int(float(row.get("like_count") or 0))
    comments = int(float(row.get("comment_count") or 0))
    return views + 8 * likes + 20 * comments


def load_candidates(
    report_csv: str | Path,
    *,
    hero: str,
    min_gameplay_score: float,
    min_duration: float,
    max_duration: float,
    blocked_sources: set[str] | None = None,
    banned_words: tuple[str, ...] = DEFAULT_BANNED_WORDS,
) -> list[ClipCandidate]:
    hero_key = hero.lower().strip()
    blocked_sources = blocked_sources or DEFAULT_BLOCKED_SOURCES
    candidates: list[ClipCandidate] = []

    with Path(report_csv).open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("is_gameplay")).lower() != "true":
                continue

            path = Path(row.get("path") or "")
            if not path.exists():
                continue

            source_label = row.get("source_label") or path.parent.name
            if source_label in blocked_sources or path.parent.name in blocked_sources:
                continue

            description = row.get("description") or ""
            description_lower = description.lower()
            if hero_key not in description_lower:
                continue
            if any(word in description_lower for word in banned_words):
                continue

            gameplay_score = float(row.get("gameplay_score") or 0.0)
            if gameplay_score < min_gameplay_score:
                continue

            try:
                duration = _probe_duration(path)
            except Exception:
                continue
            if duration < min_duration or duration > max_duration:
                continue

            candidates.append(
                ClipCandidate(
                    path=path,
                    hero=hero_key,
                    source_label=source_label,
                    description=description,
                    gameplay_score=gameplay_score,
                    popularity_score=_popularity_score(row),
                    duration=duration,
                )
            )

    candidates.sort(
        key=lambda item: (item.gameplay_score, item.popularity_score, item.duration),
        reverse=True,
    )
    return candidates


def select_scenes(candidates: list[ClipCandidate], scene_count: int) -> list[ClipCandidate]:
    selected: list[ClipCandidate] = []
    used_paths: set[Path] = set()
    for candidate in candidates:
        if candidate.path in used_paths:
            continue
        selected.append(candidate)
        used_paths.add(candidate.path)
        if len(selected) >= scene_count:
            break
    if len(selected) < scene_count:
        raise RuntimeError(f"Only found {len(selected)} usable scenes, need {scene_count}.")
    return selected


def _segment_start(duration: float, segment_duration: float, scene_index: int) -> float:
    if duration <= segment_duration + 0.6:
        return 0.0
    # Bias toward the middle of the clip, where TikTok gameplay highlights usually peak.
    fractions = (0.26, 0.34, 0.42, 0.50)
    fraction = fractions[min(scene_index, len(fractions) - 1)]
    return max(0.0, min(duration - segment_duration - 0.3, duration * fraction))


def render_montage(
    scenes: list[ClipCandidate],
    *,
    output_path: str | Path,
    target_duration: float,
    transition_duration: float,
    title: str,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = output.parent / f".{output.stem}_segments"
    work_dir.mkdir(parents=True, exist_ok=True)

    scene_count = len(scenes)
    segment_duration = (target_duration + (scene_count - 1) * transition_duration) / scene_count
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    segment_paths: list[Path] = []
    for index, scene in enumerate(scenes):
        start = _segment_start(scene.duration, segment_duration, index)
        segment_path = work_dir / f"segment_{index + 1:02d}.mp4"
        filters = [
            "scale=1080:1920:force_original_aspect_ratio=decrease",
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
            "fps=30",
            "format=yuv420p",
        ]
        if index == 0:
            filters.append(
                "drawtext=fontfile={}:text='{}':x=(w-text_w)/2:y=72:"
                "fontsize=54:fontcolor=white:borderw=4:bordercolor=black:enable='lt(t,1.8)'".format(
                    font,
                    title.replace("'", ""),
                )
            )
        filters.append(
            "drawtext=fontfile={}:text='Scene {}/{}':x=38:y=h-105:"
            "fontsize=32:fontcolor=white@0.62:borderw=3:bordercolor=black:enable='lt(t,1.5)'".format(
                font,
                index + 1,
                scene_count,
            )
        )

        audio_filter = (
            "loudnorm=I=-16:TP=-1.5:LRA=9,"
            "acompressor=threshold=-18dB:ratio=2.2:attack=10:release=120,"
            "highpass=f=80,lowpass=f=14500"
        )
        command = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.2f}",
            "-t",
            f"{segment_duration:.2f}",
            "-i",
            str(scene.path),
        ]
        if _has_audio(scene.path):
            command.extend(["-vf", ",".join(filters), "-af", audio_filter])
        else:
            command.extend(
                [
                    "-f",
                    "lavfi",
                    "-t",
                    f"{segment_duration:.2f}",
                    "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-vf",
                    ",".join(filters),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-shortest",
                ]
            )
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-ar",
                "44100",
                "-ac",
                "2",
                str(segment_path),
            ]
        )
        _run(command)
        segment_paths.append(segment_path)

    if len(segment_paths) == 1:
        _run(["ffmpeg", "-y", "-i", str(segment_paths[0]), "-movflags", "+faststart", str(output)])
        return output

    inputs: list[str] = []
    for segment_path in segment_paths:
        inputs.extend(["-i", str(segment_path)])

    video_label = "0:v"
    audio_label = "0:a"
    filters: list[str] = []
    offset = segment_duration - transition_duration
    for index in range(1, len(segment_paths)):
        next_video = f"{index}:v"
        next_audio = f"{index}:a"
        out_video = "vout" if index == len(segment_paths) - 1 else f"v{index}"
        out_audio = "aout" if index == len(segment_paths) - 1 else f"a{index}"
        filters.append(
            f"[{video_label}][{next_video}]xfade=transition=fade:duration={transition_duration}:offset={offset:.3f}[{out_video}]"
        )
        filters.append(
            f"[{audio_label}][{next_audio}]acrossfade=d={transition_duration}:c1=tri:c2=tri[{out_audio}]"
        )
        video_label = out_video
        audio_label = out_audio
        offset += segment_duration - transition_duration

    _run(
        [
            "ffmpeg",
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a vertical gameplay montage from filtered TikTok clips.")
    parser.add_argument("--hero", required=True, help="Hero name to search in descriptions/hashtags.")
    parser.add_argument("--report-csv", default="datasets/tiktok/reports/gameplay_filter_full.csv")
    parser.add_argument("--output-dir", default="datasets/outputs/montages")
    parser.add_argument("--target-duration", type=float, default=45.0)
    parser.add_argument("--scenes", type=int, default=4)
    parser.add_argument("--transition-duration", type=float, default=0.65)
    parser.add_argument("--min-gameplay-score", type=float, default=0.55)
    parser.add_argument("--min-source-duration", type=float, default=12.0)
    parser.add_argument("--max-source-duration", type=float, default=240.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not (33 <= args.target_duration <= 57):
        raise SystemExit("--target-duration should be between 33 and 57 seconds.")
    if not (3 <= args.scenes <= 4):
        raise SystemExit("--scenes should be 3 or 4.")

    candidates = load_candidates(
        args.report_csv,
        hero=args.hero,
        min_gameplay_score=args.min_gameplay_score,
        min_duration=args.min_source_duration,
        max_duration=args.max_source_duration,
    )
    scenes = select_scenes(candidates, args.scenes)
    output_dir = Path(args.output_dir)
    output = output_dir / f"{args.hero.lower()}_{args.scenes}scenes_{int(args.target_duration)}s.mp4"
    result = render_montage(
        scenes,
        output_path=output,
        target_duration=args.target_duration,
        transition_duration=args.transition_duration,
        title=f"{args.hero.upper()} GAMEPLAY",
    )
    print(f"Rendered {result}")
    print("Scenes:")
    for scene in scenes:
        print(
            "- {} | score={:.3f} | duration={:.1f} | source={} | {}".format(
                shlex.quote(str(scene.path)),
                scene.gameplay_score,
                scene.duration,
                scene.source_label,
                scene.description[:100].replace("\n", " "),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
