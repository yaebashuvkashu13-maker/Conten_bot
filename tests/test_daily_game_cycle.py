"""Tests for daily_game_cycle module."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import daily_game_cycle as cycle  # noqa: E402


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "daily_game_cycle.json"
    monkeypatch.setenv("DAILY_GAME_CYCLE_STATE", str(state))
    monkeypatch.setenv("DAILY_GAME_CYCLE_ENABLED", "1")
    monkeypatch.setenv("DAILY_MLBB_QUOTA", "10")
    monkeypatch.setenv("DAILY_PUBG_QUOTA", "10")
    monkeypatch.setenv("DAILY_STANDOFF_QUOTA", "10")
    return state


def test_active_game_starts_mlbb(isolated_state: Path) -> None:
    assert cycle.active_game() == "mlbb"
    assert cycle.quota_remaining("mlbb") == 10


def test_mlbb_quota_advances_to_pubg(isolated_state: Path) -> None:
    for _ in range(10):
        cycle.record_send("mlbb", 1)
    assert cycle.quota_remaining("mlbb") == 0
    assert cycle.active_game() == "pubg"


def test_full_rotation(isolated_state: Path) -> None:
    for _ in range(10):
        cycle.record_send("mlbb", 1)
    for _ in range(10):
        cycle.record_send("pubg", 1)
    assert cycle.active_game() == "standoff"
    for _ in range(10):
        cycle.record_send("standoff", 1)
    assert cycle.active_game() == "genshin"
    for _ in range(5):
        cycle.record_send("genshin", 1)
    assert cycle.active_game() == "wot"
    for _ in range(5):
        cycle.record_send("wot", 1)
    assert cycle.active_game() is None


def test_can_send_blocks_wrong_game(isolated_state: Path) -> None:
    cycle.record_send("mlbb", 5)
    ok, reason = cycle.can_send_for_game("pubg", 1)
    assert ok is False
    assert "wait_for_mlbb" in reason


def test_reset_new_day(isolated_state: Path) -> None:
    cycle.record_send("mlbb", 10)
    assert cycle.active_game() == "pubg"
    with patch.object(cycle, "_today_key", return_value="2099-01-02"):
        assert cycle.reset_if_new_day() is True
        assert cycle.active_game() == "mlbb"
        assert cycle.send_count("mlbb") == 0


def test_today_key_uses_moscow(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime

    class FakeDatetime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 6, 26, 23, 30, tzinfo=tz)

    monkeypatch.setattr(cycle, "datetime", FakeDatetime)
    assert cycle._today_key() == "2026-06-26"


def test_disabled_cycle_allows_all(isolated_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAILY_GAME_CYCLE_ENABLED", "0")
    ok, reason = cycle.can_send_for_game("standoff", 1)
    assert ok is True
    assert reason == "cycle_disabled"


def test_discovery_miss_skips_game_after_streak(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_GAME_DISCOVERY_MISS_SKIP", "3")
    for _ in range(10):
        cycle.record_send("mlbb", 1)
    assert cycle.active_game() == "pubg"

    assert cycle.maybe_skip_on_discovery_miss("pubg") is False
    assert cycle.maybe_skip_on_discovery_miss("pubg") is False
    assert cycle.maybe_skip_on_discovery_miss("pubg") is True

    assert cycle.quota_remaining("pubg") == 0
    assert cycle.active_game() == "standoff"
    state = cycle.load_state()
    assert state["skipped"]["pubg"]["reason"].startswith("discovery_miss")


def test_discovery_hit_clears_miss_streak(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_GAME_DISCOVERY_MISS_SKIP", "3")
    for _ in range(10):
        cycle.record_send("mlbb", 1)
    assert cycle.maybe_skip_on_discovery_miss("pubg") is False
    assert cycle.maybe_skip_on_discovery_miss("pubg") is False
    cycle.clear_discovery_miss("pubg")
    # Streak reset — need 3 fresh misses again.
    assert cycle.maybe_skip_on_discovery_miss("pubg") is False
    assert cycle.maybe_skip_on_discovery_miss("pubg") is False
    assert cycle.quota_remaining("pubg") == 10


def test_skip_game_quota_advances(isolated_state: Path) -> None:
    for _ in range(10):
        cycle.record_send("mlbb", 1)
    cycle.skip_game_quota("pubg", reason="manual")
    assert cycle.active_game() == "standoff"
    assert cycle.send_count("pubg") == 10


def test_discovery_miss_does_not_skip_last_game(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_GAME_DISCOVERY_MISS_SKIP", "2")
    monkeypatch.setenv("DAILY_PUBG_QUOTA", "0")
    monkeypatch.setenv("DAILY_STANDOFF_QUOTA", "0")
    monkeypatch.setenv("DAILY_GENSHIN_QUOTA", "0")
    monkeypatch.setenv("DAILY_WOT_QUOTA", "0")
    # Only MLBB has quota left.
    assert cycle.active_game() == "mlbb"
    assert cycle.maybe_skip_on_discovery_miss("mlbb") is False
    assert cycle.maybe_skip_on_discovery_miss("mlbb") is False
    assert cycle.quota_remaining("mlbb") == 10
    assert cycle.active_game() == "mlbb"
