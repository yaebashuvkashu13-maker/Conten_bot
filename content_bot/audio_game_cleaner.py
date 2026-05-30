from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


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
        timeout=30,
    )
    return bool(result.stdout.strip())


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
        timeout=30,
    )
    return float(json.loads(output)["format"]["duration"])


def build_ffmpeg_sfx_filter(strength: float) -> str:
    """Build a conservative SFX-preserving filter.

    This does not magically split sources. It attenuates common music-bed ranges,
    keeps high-mid transients, and uses a light gate/compressor so UI hits,
    skill sounds, and impact sounds stay more noticeable than sustained music.
    """
    strength = max(0.0, min(strength, 1.0))
    bass_cut = -5.0 - 9.0 * strength
    low_mid_cut = -3.0 - 6.0 * strength
    presence_boost = 1.5 + 3.0 * strength
    air_boost = 0.5 + 2.0 * strength
    return ",".join(
        [
            "highpass=f=85",
            "lowpass=f=15000",
            f"equalizer=f=110:width_type=o:width=1.2:g={bass_cut:.2f}",
            f"equalizer=f=260:width_type=o:width=1.1:g={low_mid_cut:.2f}",
            f"equalizer=f=650:width_type=o:width=1.0:g={low_mid_cut * 0.55:.2f}",
            f"equalizer=f=2800:width_type=o:width=1.0:g={presence_boost:.2f}",
            f"equalizer=f=5200:width_type=o:width=1.0:g={air_boost:.2f}",
            "agate=threshold=0.018:ratio=1.8:attack=5:release=90:makeup=1.15",
            "acompressor=threshold=-18dB:ratio=2.2:attack=6:release=110:makeup=1.4",
            "dynaudnorm=f=150:g=7:p=0.85",
            "alimiter=limit=0.95",
        ]
    )


def process_with_ffmpeg(input_path: Path, output_path: Path, *, strength: float) -> Path:
    if not _has_audio(input_path):
        raise RuntimeError(f"Input video has no audio stream: {input_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "copy",
            "-af",
            build_ffmpeg_sfx_filter(strength),
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


def process_with_demucs(input_path: Path, output_path: Path) -> Path:
    """Best-effort Demucs mode.

    Demucs separates music stems, not "game SFX" directly. The practical mix here
    keeps the "other" stem where many UI/game sounds land, keeps a little drums
    for impacts, and drops most bass/vocals where TikTok music usually dominates.
    """
    if shutil.which("python3") is None:
        raise RuntimeError("python3 is required for demucs mode.")
    try:
        import demucs  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("demucs is not installed. Install it with: python3 -m pip install demucs") from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = _probe_duration(input_path)
    with tempfile.TemporaryDirectory() as temp_raw:
        temp_dir = Path(temp_raw)
        audio_wav = temp_dir / "input.wav"
        _run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-vn",
                "-ac",
                "2",
                "-ar",
                "44100",
                str(audio_wav),
            ]
        )
        separated_dir = temp_dir / "separated"
        _run(["python3", "-m", "demucs", "-n", "htdemucs", "-o", str(separated_dir), str(audio_wav)])
        stem_dir = separated_dir / "htdemucs" / "input"
        other = stem_dir / "other.wav"
        drums = stem_dir / "drums.wav"
        vocals = stem_dir / "vocals.wav"
        if not other.exists():
            raise RuntimeError("Demucs did not produce an other.wav stem.")
        cleaned_wav = temp_dir / "game_sfx.wav"
        inputs = ["-i", str(other)]
        filter_parts = ["[0:a]volume=1.0[a0]"]
        amix_inputs = "[a0]"
        next_index = 1
        if drums.exists():
            inputs.extend(["-i", str(drums)])
            filter_parts.append(f"[{next_index}:a]volume=0.20[a{next_index}]")
            amix_inputs += f"[a{next_index}]"
            next_index += 1
        if vocals.exists():
            inputs.extend(["-i", str(vocals)])
            filter_parts.append(f"[{next_index}:a]volume=0.08[a{next_index}]")
            amix_inputs += f"[a{next_index}]"
            next_index += 1
        filter_parts.append(
            f"{amix_inputs}amix=inputs={next_index}:normalize=0,"
            "highpass=f=85,lowpass=f=15000,acompressor=threshold=-18dB:ratio=2.1:attack=6:release=120,"
            "dynaudnorm=f=150:g=7:p=0.85,alimiter=limit=0.95[out]"
        )
        _run(
            [
                "ffmpeg",
                "-y",
                *inputs,
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[out]",
                "-t",
                f"{duration:.3f}",
                str(cleaned_wav),
            ]
        )
        _run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-i",
                str(cleaned_wav),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reduce TikTok music while preserving game sound effects.")
    parser.add_argument("--input", required=True, help="Input video path.")
    parser.add_argument("--output", required=True, help="Output video path.")
    parser.add_argument(
        "--method",
        choices=("ffmpeg", "demucs"),
        default="ffmpeg",
        help="ffmpeg is fast and local; demucs is heavier and requires model dependencies.",
    )
    parser.add_argument("--strength", type=float, default=0.75, help="Music attenuation strength for ffmpeg mode, 0-1.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    if args.method == "ffmpeg":
        result = process_with_ffmpeg(input_path, output_path, strength=args.strength)
    else:
        result = process_with_demucs(input_path, output_path)
    print(f"Processed {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
