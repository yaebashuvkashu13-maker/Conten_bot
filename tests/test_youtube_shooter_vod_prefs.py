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
    assert not title_ok("pubg", "УЧУСЬ ИГРАТЬ В МЕТРО РОЯЛЬ | СТРИМ METRO ROYALE")
    assert not title_ok("pubg", "Learning to play PUBG Mobile Metro Royale")
    assert not title_ok("pubg", "HOW TO SPAWN NEW NINE-TAILS BOSS IN METRO ROYALE")
    assert not title_ok("pubg", "Metro Royale KARAMBIT KNIFE DROPS")
    assert not title_ok("pubg", "3.7 Metro Royale Gameplay tips")
    assert not title_ok("pubg", "Prize Path Mission Not Complete in Metro Royale")
    assert title_ok("pubg", "No Escape from the Flames! Metro Royale")
    assert title_ok("pubg", "PUBG Metro Royale 1v1 Clutch Fight")
    assert title_ok("pubg", "ЖЕСТЬ ЛУЧШИЕ КАТКИ В МЕТРО РОЯЛЬ PUBG MOBILE")
    assert title_ok(
        "pubg",
        "СОЛО ПРОТИВ ПАЧЕК НА 7 КАРТЕ В МЕТРО РОЯЛЬ ! PUBG Mobile - С ВЕБКОЙ НА РУКИ",
    )
    # Common YouTube typo "Metro Royal" + vs/squad fight wording.
    assert title_ok("pubg", "1v8 clutch PUBG(Metro Royal)")
    assert title_ok("pubg", "ZETIK VS RANDIR DUO VS SQUADS IN METRO ROYALE")
    assert title_ok("pubg", "Daily one vs squad in the Metro Royale mode")
    assert not title_ok("pubg", "ОТ НУЛЯ ДО ФУЛЛ 6 В МЕТРО РОЯЛЬ - НОЖ КЕРАМБИТ")
    assert not title_ok("pubg", "DAY 1 ZERO TO HERO STRATEGY PUBG METRO ROYALE")


def test_standoff_title_ok() -> None:
    assert title_ok("standoff", "Standoff 2 ranked clutch gameplay")
    assert not title_ok("standoff", "PUBG Metro Royale stream")


def test_discovery_rotates_queries() -> None:
    a = vod_discovery_search_cycle(0, "pubg", {})
    b = vod_discovery_search_cycle(1, "pubg", {})
    assert a["queries"] != b["queries"]
    assert len(a["queries"]) >= 1


def test_discovery_uses_env_pubg_queries() -> None:
    env = {"PUBG_VOD_SEARCH_QUERIES": "метро роял соло против сквада,метро роял пабг файт"}
    a = vod_discovery_search_cycle(0, "pubg", env)
    assert a["queries"] == ["метро роял соло против сквада", "метро роял пабг файт", "метро роял соло против сквада"]


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
