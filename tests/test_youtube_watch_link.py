import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from youtube_watch_link import youtube_watch_url, youtube_watch_url_from_row


def test_youtube_watch_url_with_timestamp():
    assert youtube_watch_url("abc123", 16.7) == "https://youtu.be/abc123?t=16"
    assert youtube_watch_url("abc123", 0.9) == "https://youtu.be/abc123"


def test_youtube_watch_url_from_row_prefers_trim():
    row = {"video_id": "vid1", "clip_start_sec": 0.1, "trim_start_sec": 22.4}
    assert youtube_watch_url_from_row(row) == "https://youtu.be/vid1?t=22"


def test_youtube_watch_url_from_row_vod_start():
    row = {"id": "hoV3DqtHS0Q", "start": 806.0}
    assert youtube_watch_url_from_row(row) == "https://youtu.be/hoV3DqtHS0Q?t=806"
