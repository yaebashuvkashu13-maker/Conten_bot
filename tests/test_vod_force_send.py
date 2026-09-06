"""Force-send one VOD feed cycle after recover."""

from __future__ import annotations

import json
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
    monkeypatch.setattr(
        "vod_force_send._run_feed_streaming",
        lambda *_a, **_k: ("info\npipeline done sent=1 vods=1 game=pubg\n", 0, False),
    )
    monkeypatch.setattr("vod_force_send._stop_game_feed", lambda _g: None)
    monkeypatch.setattr("vod_force_send.clear_feed_locks", lambda: [])
    monkeypatch.setattr("vod_feed_recover.unpark_ready_vods", lambda *a, **k: 0)
    monkeypatch.setattr("vod_feed_recover.bump_scan_cooldowns", lambda *a, **k: 0)
    row = force_send_game("pubg", stop_running=False)
    assert row["sent"] == 1


def test_force_send_game_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "vod_force_send._run_feed_streaming",
        lambda *_a, **_k: ("", None, True),
    )
    monkeypatch.setattr("vod_force_send._stop_game_feed", lambda _g: None)
    monkeypatch.setattr("vod_force_send.clear_feed_locks", lambda: [])
    monkeypatch.setattr("vod_force_send._reject_hint", lambda _g: "inbox exhausted")
    monkeypatch.setattr("vod_feed_recover.unpark_ready_vods", lambda *a, **k: 0)
    monkeypatch.setattr("vod_feed_recover.bump_scan_cooldowns", lambda *a, **k: 0)
    row = force_send_game("pubg", timeout_sec=10, stop_running=False)
    assert row["sent"] == 0
    assert "timeout" in str(row.get("error"))


def test_unpark_clears_dense_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_feed_recover import unpark_ready_vods

    inbox = tmp_path / "inbox"
    parked = tmp_path / "parked"
    inbox.mkdir()
    parked.mkdir()
    vod = parked / "yt_CCCCCCCCCCC.mp4"
    vod.write_bytes(b"x" * 50_000_000)
    state = {
        "vods": [
            {
                "id": "CCCCCCCCCCC",
                "exhausted": True,
                "reject_reason": "pubg_singles_exhausted",
                "dense_rejected_peaks": [88.0, 342.6],
                "last_pool_peaks": [
                    {"peak_sec": 88.0},
                    {"peak_sec": 342.6},
                    {"peak_sec": 666.1},
                ],
            }
        ]
    }
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    class Spec:
        def inbox(self):
            return inbox

    monkeypatch.setattr("vod_feed_recover.spec", lambda _g: Spec())
    monkeypatch.setattr(
        "vod_feed_recover.load_state",
        lambda _g: json.loads(state_path.read_text(encoding="utf-8")),
    )
    monkeypatch.setattr(
        "vod_feed_recover.save_state",
        lambda _g, st: state_path.write_text(json.dumps(st), encoding="utf-8"),
    )
    monkeypatch.setattr(
        "shooter_vod_segment_store.load_feed_sent",
        lambda _g: set(),
    )
    monkeypatch.setattr(
        "shooter_vod_segment_store.load_index",
        lambda _g: {"segments": []},
    )

    assert unpark_ready_vods("pubg", limit=1) == 1
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    row = saved["vods"][0]
    assert row["exhausted"] is False
    assert "dense_rejected_peaks" not in row
    assert (inbox / "yt_CCCCCCCCCCC.mp4").exists()
