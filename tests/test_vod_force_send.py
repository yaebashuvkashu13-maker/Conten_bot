"""Force-send one VOD feed cycle after recover."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_force_send import (  # noqa: E402
    _parse_pipeline_line,
    format_force_send_report,
    force_send_game,
)


def test_parse_pipeline_line() -> None:
    assert _parse_pipeline_line("pipeline done sent=2 vods=1 game=pubg") == {
        "sent": 2,
        "flags": "",
    }
    assert _parse_pipeline_line("pipeline done sent=0 vods=0 game=pubg inbox_cooldown=1") == {
        "sent": 0,
        "flags": "inbox_cooldown=1",
    }


def test_format_force_send_report_sent() -> None:
    text = format_force_send_report([{"game": "pubg", "sent": 1}])
    assert "отправлено 1" in text
    assert "✅" in text


def test_format_force_send_report_zero() -> None:
    text = format_force_send_report(
        [{"game": "pubg", "sent": 0, "hint": "fast_montage_need_2_have_1×5"}]
    )
    assert "не отправлено" in text
    assert "fast_montage_need_2_have_1" in text


def test_force_send_game_parses_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProc:
        returncode = 0
        stdout = "info\npipeline done sent=1 vods=1 game=pubg\n"
        stderr = ""

    monkeypatch.setattr("vod_force_send.subprocess.run", lambda *a, **k: FakeProc())
    monkeypatch.setattr("vod_force_send._stop_game_feed", lambda _g: None)
    monkeypatch.setattr("vod_force_send.clear_feed_locks", lambda: [])
    row = force_send_game("pubg", stop_running=False)
    assert row["sent"] == 1


def test_force_send_game_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def _boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="feed", timeout=10)

    monkeypatch.setattr("vod_force_send.subprocess.run", _boom)
    monkeypatch.setattr("vod_force_send._stop_game_feed", lambda _g: None)
    monkeypatch.setattr("vod_force_send.clear_feed_locks", lambda: [])
    monkeypatch.setattr("vod_force_send._reject_hint", lambda _g: "inbox exhausted")
    row = force_send_game("pubg", timeout_sec=10, stop_running=False)
    assert row["sent"] == 0
    assert "timeout" in str(row.get("error"))
