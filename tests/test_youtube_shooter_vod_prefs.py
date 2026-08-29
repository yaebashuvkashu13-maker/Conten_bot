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


def test_standoff_title_ok() -> None:
    assert title_ok("standoff", "Standoff 2 ranked clutch gameplay")
    assert not title_ok("standoff", "PUBG Metro Royale stream")


def test_discovery_rotates_queries() -> None:
    a = vod_discovery_search_cycle(0, "pubg", {})
    b = vod_discovery_search_cycle(1, "pubg", {})
    assert a["queries"] != b["queries"]
    assert len(a["queries"]) >= 1
    assert int(a["limit"]) == 80
    assert str(a["queries"][0]).lower().startswith(("метро", "пабг"))
    assert all(str(u).startswith("ytsearch") for u in a["urls"])


def test_discovery_can_use_results_url_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_YTSEARCH_ONLY", "0")
    params = vod_discovery_search_cycle(0, "pubg", {})
    assert any("youtube.com/results" in str(u) for u in params["urls"])


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
