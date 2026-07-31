#!/usr/bin/env python3
"""Decode video frames via ffmpeg (AV1/VP9 safe; OpenCV often fails on VPS)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

FFMPEG_CODECS = frozenset({"av1", "av01", "vp9", "vp09", "hevc", "hev1"})


def ffprobe_video_info(path: Path) -> dict:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-select_streams",
            "v:0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    streams = data.get("streams") or []
    return streams[0] if streams else {}


def video_codec_name(path: Path) -> str:
    return str(ffprobe_video_info(path).get("codec_name") or "").lower()


def prefer_ffmpeg_decode(path: Path) -> bool:
    if Path(path).suffix.lower() == ".f399":
        return True
    codec = video_codec_name(path)
    if codec in FFMPEG_CODECS:
        return True
    return codec not in ("h264", "avc", "avc1", "mpeg4")


def video_pixel_size(path: Path) -> tuple[int, int]:
    info = ffprobe_video_info(path)
    w = int(info.get("width") or 0)
    h = int(info.get("height") or 0)
    if w > 0 and h > 0:
        return w, h
    return 1280, 720


def ffmpeg_read_frame(
    path: Path,
    t_sec: float,
    *,
    width: int | None = None,
    height: int | None = None,
) -> np.ndarray | None:
    vf_parts: list[str] = []
    if width and height:
        vf_parts.append(f"scale={width}:{height}")
    vf = ",".join(vf_parts) if vf_parts else None

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-hwaccel",
        "none",
        "-ss",
        f"{max(0.0, t_sec):.3f}",
        "-i",
        str(path),
        "-frames:v",
        "1",
    ]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0 or not proc.stdout:
        return None
    if width and height:
        w, h = width, height
    else:
        w, h = video_pixel_size(path)
    need = w * h * 3
    if len(proc.stdout) < need:
        return None
    return np.frombuffer(proc.stdout[:need], dtype=np.uint8).reshape((h, w, 3)).copy()


def cv2_read_frame(cap, t_sec: float) -> np.ndarray | None:
    import cv2

    cap.set(cv2.CAP_PROP_POS_MSEC, float(t_sec) * 1000.0)
    ok, frame = cap.read()
    return frame if ok else None


def read_frame_at(
    path: Path,
    t_sec: float,
    cap=None,
    *,
    width: int | None = None,
    height: int | None = None,
) -> np.ndarray | None:
    if cap is not None and not prefer_ffmpeg_decode(path):
        frame = cv2_read_frame(cap, t_sec)
        if frame is not None:
            if width and height:
                import cv2

                return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
            return frame
    return ffmpeg_read_frame(path, t_sec, width=width, height=height)


def ensure_h264_mp4(
    path: Path,
    *,
    force: bool = False,
    timeout_sec: int | None = None,
) -> Path:
    """
    Transcode AV1/VP9/HEVC VODs to H.264 in-place for OpenCV + dense OCR speed.

    ffmpeg_read_frame can decode AV1, but 1 Hz dense scans spawn hundreds of
    ffmpeg processes and stall for hours. Prefer H.264 after download when
    MLBB_VOD_ENSURE_H264=1 (default).
    """
    import logging
    import os
    import shutil

    log = logging.getLogger("video_frame_io")
    path = Path(path)
    if not path.is_file():
        return path
    if os.environ.get("MLBB_VOD_ENSURE_H264", "1") != "1" and not force:
        return path
    codec = video_codec_name(path)
    if codec in ("h264", "avc", "avc1", "mpeg4") and not force:
        return path
    if codec not in FFMPEG_CODECS and not force:
        return path
    out = path.with_suffix(".h264.mp4")
    tmp = path.with_suffix(".h264.tmp.mp4")
    # Drop stale partials from interrupted runs.
    for stale in (tmp, out):
        try:
            if stale.exists() and stale.stat().st_size < 1_000_000:
                stale.unlink(missing_ok=True)
        except OSError:
            pass
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-c:v",
        "libx264",
        "-preset",
        os.environ.get("MLBB_VOD_H264_PRESET", "veryfast"),
        "-crf",
        os.environ.get("MLBB_VOD_H264_CRF", "23"),
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    to = timeout_sec
    if to is None:
        to = int(os.environ.get("MLBB_VOD_H264_TIMEOUT", "3600"))
    log.info("ensure_h264 %s codec=%s -> h264", path.name, codec or "?")
    proc = subprocess.run(cmd, check=False, timeout=max(60, to))
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 1_000_000:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        log.warning("ensure_h264 failed %s code=%s", path.name, proc.returncode)
        return path
    try:
        tmp.replace(path)
    except OSError:
        shutil.move(str(tmp), str(path))
    log.info("ensure_h264 ok %s", path.name)
    return path
