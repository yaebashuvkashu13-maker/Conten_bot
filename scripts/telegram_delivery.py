#!/usr/bin/env python3
"""Telegram-ready encode (H.264/AAC +faststart) and bounded upload queue with retry."""

from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

DEFAULT_MAX_BYTES = 49 * 1024 * 1024  # stay under Bot API 50MB video limit


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def already_telegram_ready(path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    """Skip re-encode when file is already H.264/AAC, has +faststart-ish size, under limit."""
    if not path.exists():
        return False
    if path.stat().st_size <= 0 or path.stat().st_size > max_bytes:
        return False
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_name,codec_type",
        "-of",
        "json",
        str(path),
    ]
    try:
        raw = subprocess.check_output(cmd, text=True, timeout=30)
        meta = json.loads(raw)
    except Exception:
        return False
    streams = meta.get("streams") if isinstance(meta, dict) else None
    if not isinstance(streams, list):
        return False
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v or str(v.get("codec_name") or "") not in {"h264", "avc1"}:
        return False
    if a and str(a.get("codec_name") or "") not in {"aac", "mp4a"}:
        return False
    return True


def encode_telegram_mp4(
    src: Path,
    dst: Path | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    height: int = 720,
) -> Path:
    """Encode (or copy) to Telegram-friendly MP4 with +faststart."""
    src = Path(src)
    out = Path(dst) if dst else src.with_name(src.stem + "_tg.mp4")
    if already_telegram_ready(src, max_bytes=max_bytes):
        if out.resolve() != src.resolve():
            # Remux only to ensure moov at front.
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(src),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(out),
            ]
            subprocess.run(cmd, check=False, timeout=120)
            if out.exists() and out.stat().st_size > 0:
                return out
        return src

    crf = _env_int("TELEGRAM_ENCODE_CRF", 23)
    for attempt_crf in (crf, crf + 3, crf + 6):
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-vf",
            f"scale=-2:{int(height)}",
            "-c:v",
            "libx264",
            "-preset",
            os.environ.get("TELEGRAM_ENCODE_PRESET", "veryfast"),
            "-crf",
            str(attempt_crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            os.environ.get("TELEGRAM_AAC_BITRATE", "128k"),
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(out),
        ]
        try:
            subprocess.run(cmd, check=True, timeout=600)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        if out.exists() and 0 < out.stat().st_size <= max_bytes:
            return out
    return out if out.exists() else src


def is_retryable_upload_error(exc: BaseException) -> bool:
    """Retry network/timeouts only — never re-render on client/logic errors."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    needles = (
        "timeout",
        "timed out",
        "temporarily",
        "connection",
        "connect",
        "reset by peer",
        "broken pipe",
        "503",
        "502",
        "504",
        "429",
        "retry after",
        "flood",
        "network",
        "dns",
        "ssl",
        "http 5",
    )
    if any(n in name for n in ("timeout", "connection", "network", "ssl")):
        return True
    return any(n in text for n in needles)


class TelegramUploadQueue:
    """Small in-process queue with retry/backoff to avoid parallel Bot API floods."""

    def __init__(self, *, workers: int = 1, max_attempts: int = 4) -> None:
        self.workers = max(1, int(workers))
        self.max_attempts = max(1, int(max_attempts))
        self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="tg-upload")

    def submit(
        self,
        send_fn: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ):
        def _run() -> Any:
            last_exc: Exception | None = None
            for attempt in range(1, self.max_attempts + 1):
                try:
                    result = send_fn()
                    if on_success:
                        on_success(result)
                    return result
                except Exception as exc:  # noqa: BLE001 — upload boundary
                    last_exc = exc
                    if on_error:
                        on_error(exc)
                    if attempt >= self.max_attempts or not is_retryable_upload_error(exc):
                        break
                    # Exponential backoff with light jitter; upload-only, no re-encode.
                    delay = min(60.0, (2.0**attempt)) + (0.05 * attempt)
                    time.sleep(delay)
            assert last_exc is not None
            raise last_exc

        return self._pool.submit(_run)

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)


_GLOBAL_QUEUE: TelegramUploadQueue | None = None


def get_upload_queue() -> TelegramUploadQueue:
    global _GLOBAL_QUEUE
    if _GLOBAL_QUEUE is None:
        _GLOBAL_QUEUE = TelegramUploadQueue(
            workers=_env_int("TELEGRAM_UPLOAD_WORKERS", 1),
            max_attempts=_env_int("TELEGRAM_UPLOAD_MAX_ATTEMPTS", 4),
        )
    return _GLOBAL_QUEUE
