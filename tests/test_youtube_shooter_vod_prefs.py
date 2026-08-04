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
    a = vod_discovery_search_cycle(0, "pubg", {"YOUTUBE_SEARCH_PREFER_YTSEARCH": "1"})
    b = vod_discovery_search_cycle(1, "pubg", {"YOUTUBE_SEARCH_PREFER_YTSEARCH": "1"})
    assert a["queries"] != b["queries"]
    assert len(a["queries"]) >= 1
    assert str(a["urls"][0]).startswith("ytsearch")


def test_discovery_uses_env_queries(monkeypatch) -> None:
    env = {
        "PUBG_VOD_SEARCH_QUERIES": "метро роял пабг снайпер,метро роял пабг эвакуация",
        "YOUTUBE_SEARCH_PREFER_YTSEARCH": "1",
        "MLBB_VOD_SEARCH_BATCH": "2",
    }
    got = vod_discovery_search_cycle(0, "pubg", env)
    assert got["queries"] == [
        "метро роял пабг снайпер",
        "метро роял пабг эвакуация",
    ]
    assert all(str(u).startswith("ytsearch") for u in got["urls"])


def test_build_search_targets_prefers_ytsearch() -> None:
    from youtube_download import build_search_targets, fallback_search_targets

    targets = build_search_targets(
        "PUBG Mobile Metro Royale fight",
        limit=10,
        env={"YOUTUBE_SEARCH_PREFER_YTSEARCH": "1"},
        sp="EgQQARgB",
    )
    assert targets[0].startswith("ytsearch10:")
    alts = fallback_search_targets(
        "https://www.youtube.com/results?search_query=PUBG+Mobile&sp=EgQQARgB",
        limit=10,
    )
    assert alts and alts[0].startswith("ytsearch10:")

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
