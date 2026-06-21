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


def test_hmmm_shortlive_rejected() -> None:
    reason = title_rejected_for_mlbb_shorts("Hmmm!! #shorts #shortlive")
    assert reason in ("generic_clickbait", "spam_shorts_tag")


def test_spam_shortlive_tag() -> None:
    assert title_rejected_for_mlbb_shorts("random #shortlive clip") == "spam_shorts_tag"


def test_mlbb_title_passes() -> None:
    assert title_rejected_for_mlbb_shorts("MLBB savage teamfight #shorts") is None


def test_promo_title_rejected() -> None:
    reason = title_rejected_for_mlbb_shorts("PUBG mobile highlights #shorts")
    assert reason == "other_game_title"
