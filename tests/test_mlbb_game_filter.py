"""Tests for MLBB domain conflict title gate."""

from __future__ import annotations

import sys
from pathlib import Path

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


def test_domain_conflict_codes() -> None:
    assert title_rejected_for_mlbb_shorts("PUBG mobile highlights #shorts") == "other_game_title"
    assert title_rejected_for_mlbb_shorts("Penn State Football hype") == "non_mlbb_sports"
    assert title_rejected_for_mlbb_shorts("MLBB savage teamfight #shorts") is None
