"""Tests for daily_game_cycle module."""

from __future__ import annotations

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
    monkeypatch.setenv("DAILY_MLBB_QUOTA", "5")
    monkeypatch.setenv("DAILY_PUBG_QUOTA", "5")
    monkeypatch.setenv("DAILY_STANDOFF_QUOTA", "5")
    monkeypatch.setenv("DAILY_GENSHIN_QUOTA", "2")
    monkeypatch.setenv("DAILY_WOT_QUOTA", "2")
    for legacy in (
        "DAILY_GAME_MLBB_QUOTA",
        "DAILY_GAME_PUBG_QUOTA",
        "DAILY_GAME_STANDOFF_QUOTA",
        "DAILY_GAME_GENSHIN_QUOTA",
        "DAILY_GAME_WOT_QUOTA",
    ):
        monkeypatch.delenv(legacy, raising=False)
    return state


def test_active_game_starts_mlbb(isolated_state: Path) -> None:
    assert cycle.active_game() == "mlbb"
    assert cycle.quota_remaining("mlbb") == 5


def test_mlbb_quota_advances_to_pubg(isolated_state: Path) -> None:
    for _ in range(5):
        cycle.record_send("mlbb", 1)
    assert cycle.quota_remaining("mlbb") == 0
    assert cycle.active_game() == "pubg"


def test_full_rotation(isolated_state: Path) -> None:
    for _ in range(5):
        cycle.record_send("mlbb", 1)
    for _ in range(5):
        cycle.record_send("pubg", 1)
    assert cycle.active_game() == "standoff"
    for _ in range(5):
        cycle.record_send("standoff", 1)
    assert cycle.active_game() == "genshin"
    for _ in range(2):
        cycle.record_send("genshin", 1)
    assert cycle.active_game() == "wot"
    for _ in range(2):
        cycle.record_send("wot", 1)
    assert cycle.active_game() is None


def test_can_send_blocks_wrong_game(isolated_state: Path) -> None:
    cycle.record_send("mlbb", 2)
    ok, reason = cycle.can_send_for_game("pubg", 1)
    assert ok is False
    assert "wait_for_mlbb" in reason


def test_reset_new_day(isolated_state: Path) -> None:
    cycle.record_send("mlbb", 5)
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


def test_start_next_quota_now_resets_mlbb(isolated_state: Path) -> None:
    for _ in range(5):
        cycle.record_send("mlbb", 1)
    assert cycle.active_game() == "pubg"
    summary = cycle.start_next_quota_now(notify=False)
    assert summary["active_game"] == "mlbb"
    assert summary["remaining"]["mlbb"] == 5
    assert cycle.send_count("mlbb") == 0


def test_quota_for_prefers_daily_over_legacy_game_wide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAILY_MLBB_QUOTA", "5")
    monkeypatch.setenv("DAILY_GAME_MLBB_QUOTA", "30")
    assert cycle.quota_for("mlbb") == 5


def test_defaults_match_owner_quotas(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("DAILY_") and key.endswith("_QUOTA"):
            monkeypatch.delenv(key, raising=False)
    assert cycle.quota_for("mlbb") == 5
    assert cycle.quota_for("pubg") == 5
    assert cycle.quota_for("standoff") == 5
    assert cycle.quota_for("genshin") == 2
    assert cycle.quota_for("wot") == 2
