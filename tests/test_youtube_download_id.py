"""Tests for YouTube download path / id helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from youtube_download import download_one, parse_youtube_id  # noqa: E402


def test_parse_youtube_id_watch_and_shorts() -> None:
    assert parse_youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert parse_youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert parse_youtube_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_download_one_returns_expected_id_file(tmp_path) -> None:
    dest = tmp_path / "inbox"
    dest.mkdir()
    expected = dest / "yt_dQw4w9WgXcQ.mp4"
    # Pretend a concurrent download created a newer unrelated file.
    other = dest / "yt_OTHERVIDEO1.mp4"
    other.write_bytes(b"old")

    def _fake_run(cmd, **kwargs):
        expected.write_bytes(b"new-vod")
        return None

    with patch("youtube_download.subprocess.run", side_effect=_fake_run):
        path = download_one("https://www.youtube.com/watch?v=dQw4w9WgXcQ", dest, env={})
    assert path == expected
