#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from video_frame_io import ensure_h264_mp4, prefer_ffmpeg_decode  # noqa: E402


def test_prefer_ffmpeg_for_av1() -> None:
    with patch("video_frame_io.video_codec_name", return_value="av1"):
        assert prefer_ffmpeg_decode(Path("x.mp4")) is True


def test_ensure_h264_skips_h264(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_VOD_ENSURE_H264", "1")
    vod = tmp_path / "yt_x.mp4"
    vod.write_bytes(b"0" * 3_000_000)
    with patch("video_frame_io.video_codec_name", return_value="h264"):
        with patch("video_frame_io.subprocess.run") as run:
            out = ensure_h264_mp4(vod)
            assert out == vod
            run.assert_not_called()


def test_ensure_h264_transcodes_av1(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_VOD_ENSURE_H264", "1")
    vod = tmp_path / "yt_x.mp4"
    vod.write_bytes(b"0" * 3_000_000)

    def _fake_run(cmd, check=False, timeout=3600):  # noqa: ARG001
        tmp = Path(cmd[-1])
        tmp.write_bytes(b"1" * 2_000_000)

        class P:
            returncode = 0

        return P()

    with patch("video_frame_io.video_codec_name", return_value="av1"):
        with patch("video_frame_io.subprocess.run", side_effect=_fake_run):
            out = ensure_h264_mp4(vod)
            assert out == vod
            assert vod.read_bytes()[:1] == b"1"
