"""Telegram delivery size helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_telegram_video import TELEGRAM_DOCUMENT_MAX_BYTES, TELEGRAM_MAX_BYTES  # noqa: E402


def test_telegram_limits() -> None:
    assert TELEGRAM_MAX_BYTES == 20 * 1024 * 1024
    assert TELEGRAM_DOCUMENT_MAX_BYTES >= TELEGRAM_MAX_BYTES


def test_compress_skips_small_file(tmp_path: Path) -> None:
    from mlbb_telegram_video import compress_for_inline_video

    small = tmp_path / "tiny.mp4"
    small.write_bytes(b"x" * 1024)
    out, compressed = compress_for_inline_video(small)
    assert out == small
    assert compressed is False


def test_send_video_or_document_prefers_video(monkeypatch, tmp_path: Path) -> None:
    from unittest.mock import patch

    from mlbb_telegram_video import send_video_or_document

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x" * 1024)
    with patch("mlbb_telegram_video.compress_for_inline_video", return_value=(clip, False)):
        with patch("mlbb_telegram_video.send_video_file", return_value=True) as send_vid:
            with patch("mlbb_telegram_video.send_document_file") as send_doc:
                assert send_video_or_document("tok", "1", clip, "cap")
    send_vid.assert_called_once()
    send_doc.assert_not_called()


def test_send_video_or_document_falls_back_to_file(monkeypatch, tmp_path: Path) -> None:
    from unittest.mock import patch

    from mlbb_telegram_video import TELEGRAM_MAX_BYTES, send_video_or_document

    clip = tmp_path / "big.mp4"
    clip.write_bytes(b"x" * (TELEGRAM_MAX_BYTES + 1000))
    with patch("mlbb_telegram_video.compress_for_inline_video", return_value=(clip, False)):
        with patch("mlbb_telegram_video.send_video_file", return_value=False) as send_vid:
            with patch("mlbb_telegram_video.send_document_file", return_value=True) as send_doc:
                assert send_video_or_document("tok", "1", clip, "cap")
    send_vid.assert_not_called()
    send_doc.assert_called_once()
