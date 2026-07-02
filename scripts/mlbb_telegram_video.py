#!/usr/bin/env python3
"""Split oversized calibration Shorts for Telegram Bot API (20MB limit)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TELEGRAM_MAX_BYTES = int(os.environ.get("MLBB_TELEGRAM_MAX_BYTES", str(20 * 1024 * 1024)))
TELEGRAM_DOCUMENT_MAX_BYTES = int(
    os.environ.get("MLBB_TELEGRAM_DOCUMENT_MAX_BYTES", str(50 * 1024 * 1024))
)
TARGET_PART_BYTES = int(os.environ.get("MLBB_TELEGRAM_TARGET_PART_BYTES", str(48 * 1024 * 1024)))


def _telegram_api_ok(stdout: str) -> tuple[bool, str]:
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return False, (stdout or "")[:240]
    if payload.get("ok"):
        return True, ""
    return False, str(payload.get("description") or payload)[:240]


def compress_for_inline_video(
    path: Path,
    *,
    max_bytes: int = TELEGRAM_MAX_BYTES,
) -> tuple[Path, bool]:
    """Re-encode to fit Telegram sendVideo limit (inline player with buttons)."""
    if not path.exists():
        return path, False
    if path.stat().st_size <= max_bytes:
        return path, False
    fd, tmp_name = tempfile.mkstemp(prefix="mlbb_tg_", suffix=".mp4")
    os.close(fd)
    tmp = Path(tmp_name)
    duration = max(1.0, probe_duration(path))
    target_bits = int(max_bytes * 8 * 0.90)
    audio_k = int(os.environ.get("MLBB_TG_AUDIO_K", "64"))
    audio_bps = audio_k * 1000
    video_bps = max(180_000, int(target_bits / duration) - audio_bps)

    heights = [int(x) for x in os.environ.get("MLBB_TG_SCALE_HEIGHTS", "720,540,480,360,320").split(",") if x.strip()]
    crf_steps = [x.strip() for x in os.environ.get("MLBB_TG_CRF_STEPS", "26,28,30,32,34,36,38,40,42").split(",") if x.strip()]

    for height in heights:
        scale = f"scale=-2:{height}:force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2"
        for crf in crf_steps:
            cmd = [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-hwaccel",
                "none",
                "-i",
                str(path),
                "-vf",
                scale,
                "-c:v",
                "libx264",
                "-preset",
                os.environ.get("MLBB_TG_PRESET", "veryfast"),
                "-crf",
                crf,
                "-maxrate",
                str(video_bps),
                "-bufsize",
                str(video_bps * 2),
                "-c:a",
                "aac",
                "-b:a",
                f"{audio_k}k",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                str(tmp),
            ]
            subprocess.run(cmd, check=False, timeout=600)
            if tmp.exists() and tmp.stat().st_size <= max_bytes:
                return tmp, True
    if tmp.exists() and tmp.stat().st_size > 0 and tmp.stat().st_size < path.stat().st_size:
        return tmp, True
    tmp.unlink(missing_ok=True)
    return path, False


def send_calibration_video(
    token: str,
    chat_id: str,
    path: Path,
    caption: str,
    *,
    reply_markup: dict | None = None,
) -> bool:
    """Prefer inline sendVideo (with buttons); compress if >20MB."""
    deliver, is_temp = compress_for_inline_video(path)
    try:
        if deliver.stat().st_size <= TELEGRAM_MAX_BYTES:
            ok = send_video_file(token, chat_id, deliver, caption, reply_markup=reply_markup)
            if ok:
                return True
        ok = send_hq_files(token, chat_id, path, caption, reply_markup=reply_markup)
        if not ok:
            print(f"send_calibration_video failed path={path.name} size={path.stat().st_size}")
        return ok
    finally:
        if is_temp:
            deliver.unlink(missing_ok=True)


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return max(0.5, float((proc.stdout or "0").strip()))
    except ValueError:
        return 0.0


def _encode_part(
    src: Path,
    out: Path,
    *,
    start: float,
    duration: float,
    crf: str,
) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(src),
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        os.environ.get("MLBB_SPLIT_PRESET", "medium"),
        "-crf",
        crf,
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(out),
    ]
    subprocess.run(cmd, check=True, timeout=600)


def _copy_part(src: Path, out: Path, *, start: float, duration: float | None) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src)]
    if duration is not None:
        cmd.extend(["-t", f"{duration:.3f}"])
    cmd.extend(["-c", "copy", "-movflags", "+faststart", str(out)])
    subprocess.run(cmd, check=True, timeout=300)


def split_for_telegram(
    path: Path,
    *,
    parts: int = 2,
    max_bytes: int = TELEGRAM_MAX_BYTES,
) -> list[Path]:
    """Return HQ part files, each under max_bytes when possible."""
    if not path.exists():
        return []
    if path.stat().st_size <= max_bytes:
        return [path]

    parts = max(2, parts)
    duration = probe_duration(path)
    if duration <= 0:
        return []

    chunk = duration / parts
    tmp = Path(tempfile.mkdtemp(prefix=f"mlbb_split_{path.stem}_"))
    out_paths: list[Path] = []
    crf_steps = ("17", "20", "23", "26")

    for idx in range(parts):
        start = idx * chunk
        part_dur = chunk if idx < parts - 1 else max(0.1, duration - start)
        part_path = tmp / f"{path.stem}_part{idx + 1}.mp4"

        copied = False
        try:
            _copy_part(path, part_path, start=start, duration=part_dur if idx < parts - 1 else None)
            copied = part_path.exists() and part_path.stat().st_size > 0
        except (subprocess.CalledProcessError, OSError):
            copied = False

        if copied and part_path.stat().st_size <= max_bytes:
            out_paths.append(part_path)
            continue

        encoded = False
        for crf in crf_steps:
            try:
                _encode_part(path, part_path, start=start, duration=part_dur, crf=crf)
            except (subprocess.CalledProcessError, OSError):
                continue
            if part_path.exists() and part_path.stat().st_size <= max_bytes:
                encoded = True
                break
        if not encoded and part_path.exists() and part_path.stat().st_size > 0:
            encoded = True
        if not encoded:
            return out_paths
        out_paths.append(part_path)

    return out_paths


def send_document_file(
    token: str,
    chat_id: str,
    path: Path,
    caption: str,
    *,
    filename: str | None = None,
    reply_markup: dict | None = None,
    force_file: bool | None = None,
) -> bool:
    """Send as file (no Telegram video recompression). Bot API limit ~50MB."""
    if path.stat().st_size > TELEGRAM_DOCUMENT_MAX_BYTES:
        return False
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    fname = filename or path.name
    if force_file is None:
        force_file = os.environ.get("VOD_CALIBRATION_SEND_AS_FILE", "0") == "1"
    cmd = [
        "curl",
        "-sS",
        "-m",
        "600",
        "-F",
        f"chat_id={chat_id}",
        "-F",
        f"caption={caption[:900]}",
        "-F",
        f"document=@{path};filename={fname}",
        url,
    ]
    if force_file:
        cmd.insert(-1, "-F")
        cmd.insert(-1, "disable_content_type_detection=true")
    if reply_markup:
        cmd.insert(-1, "-F")
        cmd.insert(-1, f"reply_markup={json.dumps(reply_markup, ensure_ascii=False)}")
    clean_env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    result = subprocess.run(cmd, capture_output=True, text=True, env=clean_env, timeout=620)
    ok, err = _telegram_api_ok(result.stdout)
    if not ok:
        print(f"sendDocument failed: {err}")
    return ok


def send_hq_files(
    token: str,
    chat_id: str,
    path: Path,
    caption: str,
    *,
    reply_markup: dict | None = None,
    filename: str | None = None,
) -> bool:
    """HQ delivery: always sendDocument; split only if file >50MB."""
    if not path.exists():
        return False
    if path.stat().st_size <= TELEGRAM_DOCUMENT_MAX_BYTES:
        return send_document_file(
            token,
            chat_id,
            path,
            caption,
            filename=filename,
            reply_markup=reply_markup,
            force_file=True,
        )

    parts = split_for_telegram(path, parts=2, max_bytes=TELEGRAM_DOCUMENT_MAX_BYTES)
    if not parts:
        return False
    sent_any = False
    for pi, part in enumerate(parts, start=1):
        part_cap = f"{caption}\nчасть {pi}/{len(parts)} (файл)"
        ok = send_document_file(
            token,
            chat_id,
            part,
            part_cap,
            filename=f"{path.stem}_part{pi}{path.suffix}",
            reply_markup=reply_markup if pi == len(parts) else None,
        )
        sent_any = sent_any or ok
    return sent_any


def send_video_file(
    token: str,
    chat_id: str,
    path: Path,
    caption: str,
    *,
    reply_markup: dict | None = None,
) -> bool:
    if path.stat().st_size > TELEGRAM_MAX_BYTES:
        return False
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    cmd = [
        "curl",
        "-sS",
        "-m",
        "600",
        "-F",
        f"chat_id={chat_id}",
        "-F",
        "supports_streaming=true",
        "-F",
        f"caption={caption[:900]}",
        "-F",
        f"video=@{path}",
        url,
    ]
    if reply_markup:
        cmd.insert(-1, "-F")
        cmd.insert(-1, f"reply_markup={json.dumps(reply_markup, ensure_ascii=False)}")
    clean_env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    result = subprocess.run(cmd, capture_output=True, text=True, env=clean_env, timeout=620)
    ok, err = _telegram_api_ok(result.stdout)
    if not ok:
        print(f"sendVideo failed: {err}")
    return ok


def main() -> int:
    import argparse

    from mlbb_calibration_store import find_candidate, inline_keyboard_markup
    from youtube_download import load_env

    parser = argparse.ArgumentParser(description="Split and send one calibration Short to Telegram")
    parser.add_argument("video_id", help="YouTube id, e.g. 6KDC4xrQEtY")
    parser.add_argument("--parts", type=int, default=2)
    args = parser.parse_args()

    env = {**os.environ, **load_env()}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1

    row = find_candidate(args.video_id)
    if not row:
        print(f"candidate not found: {args.video_id}", file=sys.stderr)
        return 1
    path = Path(row["path"])
    if not path.exists():
        print(f"file missing: {path}", file=sys.stderr)
        return 1

    vid = str(row.get("video_id", args.video_id))
    base_caption = (
        f"MLBB калибровка — HQ\n"
        f"score={float(row.get('score', 0)):.3f} | hook={float(row.get('hook_score', 0)):.2f}\n"
        f"views={int(row.get('view_count') or 0)}\n"
        f"{row.get('title', '')[:120]}\n"
        f"{row.get('url', '')}\n"
        f"#id {vid}\n"
        f"Нажми 👍 или 👎 под видео"
    )
    markup = inline_keyboard_markup(vid)

    ok = send_hq_files(token, chat_id, path, base_caption, reply_markup=markup)
    print("sent_hq=1" if ok else "sent_hq=0")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
