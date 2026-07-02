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
