"""Tests for shooter VOD discovery prefs."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from youtube_shooter_vod_prefs import title_ok, vod_discovery_search_cycle  # noqa: E402


def test_pubg_title_ok() -> None:
    assert title_ok("pubg", "PUBG Mobile Metro Royale ranked gameplay")
    assert title_ok("pubg", "Метро Рояль PUBG Mobile ranked перестрелка")
    assert not title_ok("pubg", "PUBG Mobile ranked gameplay full match")
    assert not title_ok("pubg", "PUBG Mobile- Metro Royale Gone Without A Trace")
    assert not title_ok("pubg", "Mobile Legends mythic ranked")
    assert not title_ok("pubg", "PUBG Metro Royale обзор гайд")


def test_pubg_default_queries_russian_first() -> None:
    from youtube_shooter_vod_prefs import default_pubg_vod_search_queries

    queries = default_pubg_vod_search_queries()
    assert len(queries) >= 18
    assert all("метро роял" in q.lower() or "metro royale" in q.lower() for q in queries)
    assert any("7 карта" in q for q in queries)
    assert any("фул 6" in q for q in queries)
    assert any("сквад" in q for q in queries)


def test_pubg_query_env_override() -> None:
    env = {"PUBG_VOD_SEARCH_QUERIES": "метро рояль тест,пабг тест"}
    a = vod_discovery_search_cycle(0, "pubg", env)
    assert "метро рояль тест" in a["queries"]


def test_standoff_title_ok() -> None:
    assert title_ok("standoff", "Standoff 2 ranked clutch gameplay")
    assert not title_ok("standoff", "PUBG Metro Royale stream")


def test_discovery_rotates_queries() -> None:
    a = vod_discovery_search_cycle(0, "pubg", {})
    b = vod_discovery_search_cycle(1, "pubg", {})
    assert a["queries"] != b["queries"]
    assert len(a["queries"]) >= 1
