"""Tests for shooter VOD discovery prefs."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from youtube_shooter_vod_prefs import (  # noqa: E402
    pick_discovery_candidate,
    title_ok,
    vod_discovery_search_cycle,
)


def test_pubg_title_ok() -> None:
    assert title_ok("pubg", "PUBG Mobile Metro Royale ranked gameplay")
    assert not title_ok("pubg", "PUBG Mobile ranked gameplay full match")
    assert not title_ok("pubg", "PUBG Mobile- Metro Royale Gone Without A Trace")
    assert not title_ok("pubg", "Mobile Legends mythic ranked")


def test_pubg_rejects_junk_titles() -> None:
    assert not title_ok("pubg", "Black market or shop in PUBG Mobile Metro Royale")
    assert not title_ok("pubg", "обмен и скам в pubg metro royale#1")
    assert not title_ok("pubg", "Is PUBG going to remove Metro Royale again?")
    assert not title_ok("pubg", "Metro Royale Flying Enemy in Rozhok Whatt ??")


def test_standoff_title_ok() -> None:
    assert title_ok("standoff", "Standoff 2 ranked clutch gameplay")
    assert not title_ok("standoff", "PUBG Metro Royale stream")


def test_discovery_rotates_queries() -> None:
    a = vod_discovery_search_cycle(0, "pubg", {})
    b = vod_discovery_search_cycle(1, "pubg", {})
    assert a["queries"] != b["queries"]
    assert len(a["queries"]) >= 1


def test_pubg_ru_stream_title_ok() -> None:
    assert title_ok("pubg", "Пабг мобайл метро рояль стрим полный матч")
    assert not title_ok("pubg", "Пабг мобайл стрим обзор гайд")


def test_pick_discovery_prefers_russian_metro(monkeypatch) -> None:
    from unittest.mock import MagicMock

    monkeypatch.setenv("SHOOTER_VOD_PREFER_RUSSIAN", "1")
    gate = MagicMock()
    gate.title_metro_hint = lambda title: "metro" in title.lower() or "метро" in title.lower()
    with patch.dict(sys.modules, {"pubg_metro_royale_gate": gate}):
        candidates = [
            {"id": "en1", "title": "PUBG Mobile Metro Royale ranked gameplay full", "duration": 900},
            {"id": "ru1", "title": "Пабг мобайл метро рояль ранкед матч стрим", "duration": 900},
        ]
        pick = pick_discovery_candidate("pubg", candidates)
    assert pick is not None
    assert pick["id"] == "ru1"


def test_pick_prefers_longer_combat(monkeypatch) -> None:
    from unittest.mock import MagicMock

    monkeypatch.setenv("SHOOTER_VOD_PREFER_RUSSIAN", "1")
    monkeypatch.setenv("SHOOTER_VOD_PREF_MIN_SEC", "600")
    gate = MagicMock()
    gate.title_metro_hint = lambda title: "metro" in title.lower() or "метро" in title.lower()
    with patch.dict(sys.modules, {"pubg_metro_royale_gate": gate}):
        candidates = [
            {"id": "short", "title": "PUBG Metro Royale shop tips", "duration": 240},
            {
                "id": "long",
                "title": "PUBG Mobile Metro Royale full match gameplay ranked",
                "duration": 900,
            },
        ]
        # short junk title may already be filtered by title_ok; pick among raw candidates
        pick = pick_discovery_candidate("pubg", candidates)
    assert pick is not None
    assert pick["id"] == "long"
