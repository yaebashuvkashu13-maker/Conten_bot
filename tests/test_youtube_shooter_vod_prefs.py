"""Tests for shooter VOD discovery prefs."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from youtube_shooter_vod_prefs import title_ok, vod_discovery_search_cycle  # noqa: E402


def test_pubg_title_ok() -> None:
    assert title_ok("pubg", "PUBG Mobile Metro Royale ranked gameplay")
    assert not title_ok("pubg", "PUBG Mobile ranked gameplay full match")
    assert not title_ok("pubg", "PUBG Mobile- Metro Royale Gone Without A Trace")
    assert not title_ok("pubg", "Mobile Legends mythic ranked")
    assert not title_ok("pubg", "METRO ROYALE FREE KARAMBIT KNIFE IN MYSTERIOUS VOUCHER")
    assert not title_ok("pubg", "HOW TO GET NEW KARAMBIT BLAZING SUN PUBG METRO ROYALE")
    assert not title_ok("pubg", "NEW METRO ROYALE MAP!")
    assert not title_ok("pubg", "METRO ROYALE OPEN 20 GOLD TICKETS FOR FABLED MK14")
    assert title_ok("pubg", "No Escape from the Flames! Metro Royale")
    assert title_ok("pubg", "PUBG Metro Royale 1v1 Clutch Fight")


def test_standoff_title_ok() -> None:
    assert title_ok("standoff", "Standoff 2 ranked clutch gameplay")
    assert not title_ok("standoff", "PUBG Metro Royale stream")


def test_discovery_rotates_queries() -> None:
    a = vod_discovery_search_cycle(0, "pubg", {})
    b = vod_discovery_search_cycle(1, "pubg", {})
    assert a["queries"] != b["queries"]
    assert len(a["queries"]) >= 1


def test_discovery_rotates_filter_modes() -> None:
    a = vod_discovery_search_cycle(0, "pubg", {"MLBB_VOD_SEARCH_FRESH": "1", "MLBB_VOD_YOUTUBE_DURATION_FILTER": "1"})
    b = vod_discovery_search_cycle(1, "pubg", {"MLBB_VOD_SEARCH_FRESH": "1", "MLBB_VOD_YOUTUBE_DURATION_FILTER": "1"})
    c = vod_discovery_search_cycle(2, "pubg", {"MLBB_VOD_SEARCH_FRESH": "1", "MLBB_VOD_YOUTUBE_DURATION_FILTER": "1"})
    assert a["filter_mode"] == "fresh_month"
    assert b["filter_mode"] == "duration_4_20"
    assert c["filter_mode"] == "fresh_week"
    assert a["sp"] and b["sp"] and a["sp"] != b["sp"]


def test_pubg_recorded_stream_title_ok() -> None:
    assert title_ok("pubg", "метро рояль пабг мобайл стрим полный матч")


def test_pubg_ru_stream_title_ok() -> None:
    assert title_ok("pubg", "Пабг мобайл метро рояль стрим полный матч")
    assert not title_ok("pubg", "Пабг мобайл стрим обзор гайд")


def test_pick_discovery_prefers_russian_metro(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from youtube_shooter_vod_prefs import pick_discovery_candidate

    monkeypatch.setenv("SHOOTER_VOD_PREFER_RUSSIAN", "1")
    gate = MagicMock()
    gate.title_metro_hint = lambda title: "metro" in title.lower() or "метро" in title.lower()
    with patch.dict(sys.modules, {"pubg_metro_royale_gate": gate}):
        candidates = [
            {"id": "en1", "title": "PUBG Mobile Metro Royale ranked gameplay full"},
            {"id": "ru1", "title": "Пабг мобайл метро рояль ранкед матч стрим"},
        ]
        pick = pick_discovery_candidate("pubg", candidates)
    assert pick is not None
    assert pick["id"] == "ru1"
