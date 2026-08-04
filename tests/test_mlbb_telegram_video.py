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


def test_compress_refuses_potato_bitrate(tmp_path: Path, monkeypatch) -> None:
    """Long clips must not be crushed to ~0.5Mbps for the 20MB inline cap."""
    from mlbb_telegram_video import compress_for_inline_video
    import mlbb_telegram_video as mtv

    big = tmp_path / "long.mp4"
    big.write_bytes(b"x" * (30 * 1024 * 1024))
    monkeypatch.setattr(mtv, "probe_duration", lambda _p: 250.0)
    monkeypatch.setenv("MLBB_TG_ALLOW_POTATO", "0")
    monkeypatch.setenv("MLBB_TG_MIN_VIDEO_BPS", "1200000")
    out, compressed = compress_for_inline_video(big, max_bytes=20 * 1024 * 1024)
    assert out == big
    assert compressed is False
