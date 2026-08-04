"""Tests for daily_cycle_runner quota notifications."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_daily_cycle_notifies_quotas_done_once(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".video_bot.env"
    env_file.write_text("TG_BOT_TOKEN=tok\nTG_CHAT_ID=1\nDAILY_GAME_CYCLE_ENABLED=1\n", encoding="utf-8")
    state_file = tmp_path / "daily_game_cycle.json"
    monkeypatch.setenv("DAILY_GAME_CYCLE_STATE", str(state_file))
    monkeypatch.setenv("DAILY_GAME_CYCLE_ENABLED", "1")
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "1")

    import daily_game_cycle as cycle
    import daily_cycle_runner as runner

    monkeypatch.setattr(runner, "ENV_PATH", env_file)

    for game in cycle.GAME_ORDER:
        quota = cycle.quota_for(game)
        cycle.record_send(game, quota)

    sent: list[str] = []

    def fake_send(token: str, chat_id: str, text: str) -> None:
        sent.append(text)

    with patch("mlbb_vod_segment_feed.send_message", fake_send):
        assert runner.main() == 0
        assert runner.main() == 0

    assert len(sent) == 1
    assert "выполнены" in sent[0]
