"""Owner Telegram process / reset controls."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from telegram_owner_controls import (  # noqa: E402
    CALLBACK_HANG_AGENT,
    CALLBACK_PROCESS,
    CALLBACK_RECOVER,
    CALLBACK_RESET,
    CALLBACK_SEND_NOW,
    DEFAULT_VOD_SEARCH_BATCH,
    DEFAULT_VOD_SEARCH_LIMIT,
    discovery_start_text,
    format_process_report,
    is_process_command,
    is_recover_command,
    is_reset_command,
    owner_controls_keyboard,
    parse_recover_game,
    parse_reset_game,
    run_recover,
    run_reset,
    run_send_now,
    scan_start_text,
)


def test_legacy_callback_ids_stable() -> None:
    assert CALLBACK_PROCESS == "ops_process"
    assert CALLBACK_RESET == "ops_reset"
    assert CALLBACK_RECOVER == "ops_recover"
    assert CALLBACK_SEND_NOW == "ops_send_now"
    assert CALLBACK_HANG_AGENT == "ops_hang_agent"


def test_owner_controls_keyboard_has_hang_agent() -> None:
    kb = owner_controls_keyboard()
    buttons = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert buttons[0] == "ops_hang_agent"
    assert "ops_process" in buttons
    assert "ops_recover" in buttons
    assert "ops_send_now" in buttons
    assert "ops_reset" in buttons


def test_text_hang_agent_command() -> None:
    from telegram_owner_controls import is_hang_agent_command

    assert is_hang_agent_command("/agent")
    assert is_hang_agent_command("/завис")
    assert is_hang_agent_command("снова завис")
    assert is_hang_agent_command("агент зависания")
    assert not is_hang_agent_command("/recover")


def test_text_process_command() -> None:
    assert is_process_command("/process")
    assert is_process_command("/процесс")
    assert is_process_command("процесс")
    assert is_process_command("process")
    assert not is_process_command("/make")


def test_text_reset_command() -> None:
    assert is_reset_command("/reset")
    assert is_reset_command("/reset pubg")
    assert is_reset_command("сброс")
    assert is_reset_command("сброс процесса")
    assert not is_reset_command("/status")


def test_text_recover_command() -> None:
    assert is_recover_command("/recover")
    assert is_recover_command("/recover pubg")
    assert is_recover_command("/fix")
    assert is_recover_command("нет видео")
    assert not is_recover_command("/reset")


def test_parse_recover_game() -> None:
    assert parse_recover_game("/recover") == "all"
    assert parse_recover_game("/recover pubg") == "pubg"
    with pytest.raises(ValueError):
        parse_recover_game("/recover fortnite")


def test_parse_reset_game() -> None:
    assert parse_reset_game("/reset") == "all"
    assert parse_reset_game("/reset pubg") == "pubg"
    assert parse_reset_game("/reset млбб") == "mlbb"
    with pytest.raises(ValueError):
        parse_reset_game("/reset fortnite")


def test_process_report_includes_games() -> None:
    text = format_process_report(
        running={"telegram_bot": True, "mlbb_feed": False},
        rows=[
            {
                "game": "mlbb",
                "inbox": 4,
                "actionable_inbox": 0,
                "exhausted_inbox": 4,
                "streak": 5,
                "daily_sent": 1,
                "daily_quota_left": 9,
                "hint": "all inbox exhausted — reset after gate fix",
            }
        ],
    )
    assert "📊 Процесс" in text
    assert "бот Telegram: работает" in text
    assert "MLBB" in text
    assert "исчерпано=4" in text
    assert "/reset" in text


def test_run_reset_clears_exhausted_and_search_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "mlbb"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "yt_abc123xyz00.mp4").write_bytes(b"x")
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps(
            {
                "vods": [{"id": "abc123xyz00", "exhausted": True, "reject_reason": "no_combat_peaks"}],
                "discovery_query_offset": 12,
                "discovery_search_cycle": 4,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MLBB_DATA_ROOT", str(root))
    msg = run_reset("mlbb")
    assert "1 VOD" in msg
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["vods"][0]["exhausted"] is False
    assert state["discovery_query_offset"] == 0
    assert state["discovery_search_cycle"] == 0


def test_search_defaults_are_high_enough_to_find_vods() -> None:
    assert DEFAULT_VOD_SEARCH_LIMIT >= 80
    assert DEFAULT_VOD_SEARCH_BATCH >= 10
    text = discovery_start_text("pubg", batch=10, limit=80)
    assert "10 запросов" in text
    assert "80 результатов" in text
    assert "Ищу" in text
    assert "Сканирую MLBB" in scan_start_text("mlbb", "abc123xyz00")


def test_run_send_now_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    import vod_force_send

    monkeypatch.setattr(
        vod_force_send,
        "force_send",
        lambda game: [{"game": "pubg", "sent": 0, "hint": "test_hint"}],
    )
    msg = run_send_now("pubg")
    assert "test_hint" in msg


def test_run_hang_agent_clears_stale_lock_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telegram_owner_controls import run_hang_agent

    lock = tmp_path / "hang.lock"
    lock.write_text("999999", encoding="utf-8")
    monkeypatch.setenv("VOD_HANG_RECOVER_LOCK", str(lock))

    class _R:
        ok = False
        last_send_age_sec = 9000.0
        heartbeat_age_sec = 100.0
        reasons = ["absolute_silence_9000s"]
        zero_send_streak = 0

    monkeypatch.setattr(
        "vod_hang_detector.detect_hang",
        lambda: _R(),
    )
    monkeypatch.setattr(
        "vod_hang_detector.auto_unload_and_recover",
        lambda *a, **k: {"action": "full_recover", "actions": ["unpark_pubg=1"], "escalation": 0},
    )
    monkeypatch.setattr(
        "vod_hang_detector.apply_agent_recover_env",
        lambda env, escalation=0: env,
    )
    monkeypatch.setattr(
        "vod_force_send.force_send",
        lambda game: [{"game": "pubg", "sent": 1, "hint": "ok"}],
    )
    monkeypatch.setattr(
        "vod_force_send.format_force_send_report",
        lambda rows: "SENT ok",
    )
    msg = run_hang_agent("pubg")
    assert "Агент зависания" in msg
    assert "снял зависший recover-lock" in msg
    assert "HANG" in msg
    assert "SENT ok" in msg
    assert not lock.exists()


def test_shooter_default_search_limit() -> None:
    from youtube_shooter_vod_prefs import vod_discovery_search_cycle

    params = vod_discovery_search_cycle(0, "pubg", {})
    assert int(params["limit"]) == 80
    assert int(params["batch"]) == 10
    assert len(params["queries"]) == 10
    assert any("метро" in q.lower() or "пабг" in q.lower() for q in params["queries"])
