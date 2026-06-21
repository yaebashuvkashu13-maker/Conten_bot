"""Tests for MLBB-only Shorts title gate."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_shorts_title_gate import NON_MLBB_SPORTS_TITLE, OTHER_GAME_TITLE, title_rejected_for_mlbb_shorts


def test_other_game_title_rejected() -> None:
    assert OTHER_GAME_TITLE.search("PUBG insane clutch shorts")
    assert OTHER_GAME_TITLE.search("Standoff 2 montage")
    assert not OTHER_GAME_TITLE.search("MLBB savage teamfight shorts")


def test_sports_title_rejected() -> None:
    assert NON_MLBB_SPORTS_TITLE.search("Penn State Football hype video")
    assert NON_MLBB_SPORTS_TITLE.search("Champions League goal shorts")
    assert not NON_MLBB_SPORTS_TITLE.search("MLBB savage teamfight shorts")


def test_football_short_rejected_by_title(tmp_path: Path) -> None:
    fake = tmp_path / "yt_test.mp4"
    fake.write_bytes(b"x" * 1000)
    reason = title_rejected_for_mlbb_shorts("Penn State Football hype video #shorts")
    assert reason == "non_mlbb_sports"


def test_promo_title_rejected() -> None:
    reason = title_rejected_for_mlbb_shorts("PUBG mobile highlights #shorts")
    assert reason == "other_game_title"
