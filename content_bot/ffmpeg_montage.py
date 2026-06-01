from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .scene_analysis import SceneSegment


def _escape_drawtext(text: str) -> str:
    return text.replace(":", r"\:").replace("'", r"\'")


def extract_segment(segment: SceneSegment, output_path: Path, *, fade_sec: float = 0.35) -> None:
    """Extract clip with short fade in/out so scenes do not cut abruptly."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = segment.duration_sec
    fade = min(fade_sec, max(duration / 4.0, 0.1))
    fade_out_start = max(duration - fade, 0.01)

    vf = f"fade=t=in:st=0:d={fade:.3f},fade=t=out:st={fade_out_start:.3f}:d={fade:.3f}"
    af = f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={fade_out_start:.3f}:d={fade:.3f}"

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{segment.start_sec:.3f}",
        "-i",
        str(segment.path),
        "-t",
        f"{duration:.3f}",
        "-vf",
        vf,
        "-af",
        af,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def assemble_montage(
    segments: list[SceneSegment],
    output_path: Path,
    *,
    transition_duration: float,
    hook_text: str | None = None,
    clip_fade_sec: float = 0.35,
) -> None:
    if not segments:
        raise RuntimeError("No segments to assemble.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="montage_") as temp_dir:
        temp = Path(temp_dir)
        clip_paths: list[Path] = []
        durations: list[float] = []

        for index, segment in enumerate(segments):
            clip_path = temp / f"clip_{index:02d}.mp4"
            extract_segment(segment, clip_path, fade_sec=clip_fade_sec)
            clip_paths.append(clip_path)
            durations.append(segment.duration_sec)

        inputs: list[str] = []
        for clip in clip_paths:
            inputs.extend(["-i", str(clip)])

        filters: list[str] = []
        audio_map = "0:a"

        if len(clip_paths) == 1:
            video_label = "[0:v]"
        else:
            video_prev = "[0:v]"
            audio_prev = "[0:a]"
            offset = durations[0] - transition_duration
            for index in range(1, len(clip_paths)):
                is_last = index == len(clip_paths) - 1
                v_label = "[vout]" if is_last else f"[vx{index}]"
                a_label = "[aout]" if is_last else f"[ax{index}]"
                filters.append(
                    f"{video_prev}[{index}:v]xfade=transition=fade:duration={transition_duration:.3f}"
                    f":offset={max(offset, 0.0):.3f}{v_label}"
                )
                filters.append(
                    f"{audio_prev}[{index}:a]acrossfade=d={transition_duration:.3f}:c1=tri:c2=tri{a_label}"
                )
                video_prev = v_label
                audio_prev = a_label
                offset += durations[index] - transition_duration
            video_label = video_prev
            audio_map = audio_prev

        if hook_text:
            safe = _escape_drawtext(hook_text)
            filters.append(
                f"{video_label}drawtext=text='{safe}':fontsize=48:fontcolor=white:"
                f"x=(w-text_w)/2:y=h*0.08:box=1:boxcolor=black@0.45:boxborderw=12[vfinal]"
            )
            video_map = "[vfinal]"
        else:
            video_map = video_label if video_label.startswith("[") else "[0:v]"

        filter_complex = ";".join(filters) if filters else None
        cmd = ["ffmpeg", "-y", *inputs]
        if filter_complex:
            cmd.extend(["-filter_complex", filter_complex, "-map", video_map])
            cmd.extend(["-map", audio_map])
        else:
            cmd.extend(["-map", "0:v", "-map", "0:a"])

        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "19",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        subprocess.run(cmd, check=True, capture_output=True)
