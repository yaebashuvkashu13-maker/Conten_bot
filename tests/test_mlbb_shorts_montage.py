"""Tests for MLBB shorts montage helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_shorts_montage import (  # noqa: E402
    _chunk,
    is_short_source,
    mlbb_shorts_env,
)


def test_chunk_splits_batches() -> None:
    items = [Path(f"a{i}.mp4") for i in range(5)]
    assert len(_chunk(items, 2)) == 3
    assert len(_chunk(items, 4)) == 2


def test_is_short_source_duration_gate(tmp_path: Path) -> None:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"x")
    with patch("mlbb_shorts_montage.ffprobe_duration", return_value=45.0):
        assert is_short_source(p) is True
    with patch("mlbb_shorts_montage.ffprobe_duration", return_value=400.0):
        assert is_short_source(p) is False


def test_mlbb_shorts_env_auto_send() -> None:
    env = mlbb_shorts_env("123", "token")
    assert env["MLBB_SHORTS_AUTO_SEND"] == "1"
    assert env["OWNER_PREVIEW_APPROVED"] == "1"
    assert env["DEFAULT_GAME_PROFILE"] == "mobile_legends"
    assert env["HIGHLIGHT_SCORER"] == "0"
