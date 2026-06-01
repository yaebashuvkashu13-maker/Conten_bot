from __future__ import annotations

import argparse
import json
import math
import shlex
import subprocess
from pathlib import Path


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def probe_duration(path: str | Path) -> float:
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
        timeout=30,
    )
    return float(json.loads(output)["format"]["duration"])


def planned_short_count(
    duration_sec: float,
    *,
    minutes_per_short: float,
    min_shorts: int,
    max_shorts: int,
) -> int:
    if duration_sec <= 0:
        return 0
    count = math.ceil(duration_sec / (minutes_per_short * 60.0))
    return max(min_shorts, min(max_shorts, count))


def _scene_starts(
    *,
    video_duration: float,
    short_index: int,
    short_count: int,
    scene_count: int,
    scene_duration: float,
) -> list[float]:
    if video_duration <= scene_duration:
        return [0.0] * scene_count

    window_start = video_duration * short_index / short_count
    window_end = video_duration * (short_index + 1) / short_count
    window_duration = max(window_end - window_start, scene_duration)

    starts: list[float] = []
    for scene_index in range(scene_count):
        fraction = (scene_index + 1) / (scene_count + 1)
        start = window_start + window_duration * fraction - scene_duration / 2
        starts.append(max(0.0, min(video_duration - scene_duration - 0.2, start)))
    return starts


def render_short(
    *,
    input_path: Path,
    output_path: Path,
    starts: list[float],
    target_duration: float,
    transition_duration: float,
    title: str,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = output_path.parent / f".{output_path.stem}_segments"
    work_dir.mkdir(parents=True, exist_ok=True)

    scene_count = len(starts)
    scene_duration = (target_duration + (scene_count - 1) * transition_duration) / scene_count
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    segments: list[Path] = []

    for index, start in enumerate(starts):
        segment_path = work_dir / f"segment_{index + 1:02d}.mp4"
        filters = [
            "scale=1080:1920:force_original_aspect_ratio=decrease",
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
            "fps=30",
            "format=yuv420p",
        ]
        if index == 0:
            filters.append(
                "drawtext=fontfile={}:text='{}':x=(w-text_w)/2:y=72:fontsize=48:"
                "fontcolor=white:borderw=4:bordercolor=black:enable='lt(t,1.4)'".format(
                    font,
                    title.replace("'", ""),
                )
            )
        filters.append(
            "drawtext=fontfile={}:text='Scene {}/{}':x=38:y=h-105:fontsize=30:"
            "fontcolor=white@0.62:borderw=3:bordercolor=black:enable='lt(t,1.2)'".format(
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
        _run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{start:.2f}",
                "-t",
                f"{scene_duration:.2f}",
                "-i",
                str(input_path),
                "-vf",
                ",".join(filters),
                "-af",
                audio_filter,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
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
        segments.append(segment_path)

    if len(segments) == 1:
        _run(["ffmpeg", "-y", "-i", str(segments[0]), "-movflags", "+faststart", str(output_path)])
        return output_path

    inputs: list[str] = []
    for segment in segments:
        inputs.extend(["-i", str(segment)])

    video_label = "0:v"
    audio_label = "0:a"
    filters = []
    offset = scene_duration - transition_duration
    for index in range(1, len(segments)):
        out_video = "vout" if index == len(segments) - 1 else f"v{index}"
        out_audio = "aout" if index == len(segments) - 1 else f"a{index}"
        filters.append(
            f"[{video_label}][{index}:v]xfade=transition=fade:duration={transition_duration}:offset={offset:.3f}[{out_video}]"
        )
        filters.append(f"[{audio_label}][{index}:a]acrossfade=d={transition_duration}:c1=tri:c2=tri[{out_audio}]")
        video_label = out_video
        audio_label = out_audio
        offset += scene_duration - transition_duration

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
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return output_path


def build_shorts(
    *,
    input_path: str | Path,
    output_dir: str | Path,
    target_duration: float,
    scene_count: int,
    minutes_per_short: float,
    min_shorts: int,
    max_shorts: int,
    transition_duration: float,
    title_prefix: str,
) -> list[Path]:
    source = Path(input_path)
    duration = probe_duration(source)
    short_count = planned_short_count(
        duration,
        minutes_per_short=minutes_per_short,
        min_shorts=min_shorts,
        max_shorts=max_shorts,
    )
    scene_duration = (target_duration + (scene_count - 1) * transition_duration) / scene_count
    output_base = Path(output_dir)
    output_base.mkdir(parents=True, exist_ok=True)

    outputs: list[Path] = []
    for index in range(short_count):
        starts = _scene_starts(
            video_duration=duration,
            short_index=index,
            short_count=short_count,
            scene_count=scene_count,
            scene_duration=scene_duration,
        )
        output = output_base / f"{source.stem}_short_{index + 1:02d}.mp4"
        print(
            f"Rendering short {index + 1}/{short_count}: {output} starts="
            + ",".join(f"{start:.1f}" for start in starts),
            flush=True,
        )
        outputs.append(
            render_short(
                input_path=source,
                output_path=output,
                starts=starts,
                target_duration=target_duration,
                transition_duration=transition_duration,
                title=f"{title_prefix} {index + 1}/{short_count}",
            )
        )
    return outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Turn a long stream/video into multiple vertical shorts.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", default="datasets/outputs/long_video_shorts")
    parser.add_argument("--target-duration", type=float, default=45.0)
    parser.add_argument("--scenes", type=int, default=4)
    parser.add_argument("--minutes-per-short", type=float, default=30.0)
    parser.add_argument("--min-shorts", type=int, default=1)
    parser.add_argument("--max-shorts", type=int, default=20)
    parser.add_argument("--transition-duration", type=float, default=0.65)
    parser.add_argument("--title-prefix", default="MLBB SHORT")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not (33 <= args.target_duration <= 57):
        raise SystemExit("--target-duration should be between 33 and 57 seconds.")
    if not (3 <= args.scenes <= 4):
        raise SystemExit("--scenes should be 3 or 4.")
    outputs = build_shorts(
        input_path=args.input,
        output_dir=args.output_dir,
        target_duration=args.target_duration,
        scene_count=args.scenes,
        minutes_per_short=args.minutes_per_short,
        min_shorts=args.min_shorts,
        max_shorts=args.max_shorts,
        transition_duration=args.transition_duration,
        title_prefix=args.title_prefix,
    )
    print("Rendered outputs:")
    for output in outputs:
        print(f"- {shlex.quote(str(output))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
