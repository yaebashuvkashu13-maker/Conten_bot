"""Tests for Genshin / WoT YouTube VOD discovery prefs."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from youtube_extended_vod_prefs import title_ok, vod_discovery_search_cycle  # noqa: E402


def test_genshin_title_ok() -> None:
    assert title_ok("genshin", "Genshin Impact boss fight gameplay")
    assert not title_ok("genshin", "Genshin wish banner guide")


def test_wot_title_ok() -> None:
    assert title_ok("wot", "WoT Blitz ranked frag gameplay")
    assert not title_ok("wot", "premium shop giveaway tanks")


def test_discovery_cycle() -> None:
    params = vod_discovery_search_cycle(0, "genshin", {})
    assert params["queries"]
    assert params["urls"]
