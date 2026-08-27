"""Owner Telegram process / reset controls."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from telegram_owner_controls import (  # noqa: E402
    BTN_PROCESS,
    BTN_RESET,
    CALLBACK_PROCESS,
    CALLBACK_RESET,
    DEFAULT_VOD_SEARCH_BATCH,
    DEFAULT_VOD_SEARCH_LIMIT,
    discovery_start_text,
    format_process_report,
    is_process_command,
    is_reset_command,
    owner_reply_keyboard,
    parse_reset_game,
    process_inline_keyboard,
    run_reset,
    scan_start_text,
)


def test_keyboard_has_process_and_reset() -> None:
    kb = owner_reply_keyboard()
    texts = [btn["text"] for row in kb["keyboard"] for btn in row]
    assert BTN_PROCESS in texts
    assert BTN_RESET in texts
    assert "Процесс" in texts
    assert "Сброс" in texts
    assert kb["resize_keyboard"] is True
    assert kb.get("one_time_keyboard") is False


def test_inline_reset_callback() -> None:
    markup = process_inline_keyboard()
    flat = [b for row in markup["inline_keyboard"] for b in row]
    data = {b["callback_data"] for b in flat}
    assert CALLBACK_RESET in data
    assert CALLBACK_PROCESS in data
    assert len(flat) == 2


def test_button_text_is_process_command() -> None:
    assert is_process_command(BTN_PROCESS)
    assert is_process_command("/process")
    assert is_process_command("/процесс")
    assert is_process_command("Процесс")
    assert not is_process_command("/make")


def test_button_text_is_reset_command() -> None:
    assert is_reset_command(BTN_RESET)
    assert is_reset_command("/reset")
    assert is_reset_command("/reset pubg")
    assert is_reset_command("Сброс")
    assert is_reset_command("сброс процесса")
    assert not is_reset_command("/status")


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
    assert "Сброс" in text


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
    assert DEFAULT_VOD_SEARCH_LIMIT >= 50
    assert DEFAULT_VOD_SEARCH_BATCH >= 6
    text = discovery_start_text("mlbb", batch=6, limit=50)
    assert "6 запросов" in text
    assert "50 результатов" in text
    assert "Ищу" in text
    assert "Сканирую MLBB" in scan_start_text("mlbb", "abc123xyz00")


def test_shooter_default_search_limit() -> None:
    from youtube_shooter_vod_prefs import vod_discovery_search_cycle

    params = vod_discovery_search_cycle(0, "pubg", {})
    assert int(params["limit"]) == 50
    assert int(params["batch"]) == 6
    assert len(params["queries"]) == 6
