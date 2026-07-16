"""Night-safety checks for daily cycle idle notify latch."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import daily_cycle_runner as runner  # noqa: E402
import daily_game_cycle as cycle  # noqa: E402


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "daily_game_cycle.json"
    monkeypatch.setenv("DAILY_GAME_CYCLE_STATE", str(state))
    monkeypatch.setenv("DAILY_GAME_CYCLE_ENABLED", "1")
    for g, q in (
        ("MLBB", "5"),
        ("PUBG", "5"),
        ("STANDOFF", "5"),
        ("GENSHIN", "2"),
        ("WOT", "2"),
    ):
        monkeypatch.setenv(f"DAILY_{g}_QUOTA", q)
    return state


def test_idle_quota_notify_once(isolated_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for g in cycle.GAME_ORDER:
        for _ in range(cycle.quota_for(g)):
            cycle.record_send(g, 1)
    assert cycle.active_game() is None

    sends: list[str] = []

    def fake_send(token, chat_id, text):
        sends.append(text)

    monkeypatch.setattr(runner, "enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_load_runtime_env",
        lambda: {"TG_BOT_TOKEN": "t", "TG_CHAT_ID": "1", "DAILY_GAME_CYCLE_ENABLED": "1"},
    )
    with patch.dict(sys.modules, {"mlbb_vod_segment_feed": MagicMock(send_message=fake_send)}):
        assert runner.main() == 0
        assert len(sends) == 1
        assert "квоты" in sends[0].lower() or "Квоты" in sends[0] or "выполнены" in sends[0]
        assert runner.main() == 0
        assert len(sends) == 1
