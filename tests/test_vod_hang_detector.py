"""Tests for VOD hang detector — real silence/progress signals."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_hang_detector import (  # noqa: E402
    detect_hang,
    heartbeat_path,
    last_send_age_sec,
    read_heartbeat,
    unload_stuck_inbox_vod,
    write_heartbeat,
    zero_send_streak,
)


def test_last_send_age_from_log_timestamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "feed.log"
    old_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 7200))
    log.write_text(f"{old_ts} INFO pipeline done sent=1 vods=1 game=pubg\n", encoding="utf-8")
    monkeypatch.setattr("vod_hang_detector.feed_log_path", lambda: log)
    monkeypatch.setattr("vod_hang_detector.spec", lambda g: type("S", (), {"feed_sent_path": lambda self: tmp_path / "missing.json"})())
    age = last_send_age_sec()
    assert age >= 7000
    assert age < 8000


def test_zero_send_streak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "feed.log"
    lines = [
        "2026-09-03 10:00:00 pipeline done sent=0 vods=1 game=pubg metro_reject=1",
        "2026-09-03 10:05:00 pipeline done sent=0 vods=1 game=pubg",
        "2026-09-03 10:10:00 pipeline done sent=1 vods=1 game=pubg",
        "2026-09-03 10:15:00 pipeline done sent=0 vods=1 game=pubg",
        "2026-09-03 10:20:00 pipeline done sent=0 vods=1 game=pubg",
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr("vod_hang_detector.feed_log_path", lambda: log)
    assert zero_send_streak() == 2


def test_heartbeat_write_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hb = tmp_path / "hb.json"
    monkeypatch.setattr("vod_hang_detector.heartbeat_path", lambda: hb)
    write_heartbeat("pubg", "scan_done", sent=1)
    data = read_heartbeat()
    assert data["game"] == "pubg"
    assert data["phase"] == "scan_done"
    assert data["sent"] == 1


def test_detect_hang_silence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "feed.log"
    old_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 4000))
    log.write_text(
        f"{old_ts} pipeline done sent=1 vods=1 game=pubg\n"
        + "\n".join(
            f"2026-09-03 12:{i:02d}:00 pipeline done sent=0 vods=1 game=pubg"
            for i in range(8)
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VOD_SILENCE_WARN_SEC", "3600")
    monkeypatch.setenv("VOD_ZERO_SEND_STREAK_HEAL", "6")
    monkeypatch.setattr("vod_hang_detector.feed_log_path", lambda: log)
    monkeypatch.setattr("vod_hang_detector.feed_process_alive", lambda: True)
    monkeypatch.setattr("vod_hang_detector.find_stuck_children", lambda *a, **k: [])
    monkeypatch.setattr("vod_hang_detector.find_stuck_part_files", lambda *a, **k: [])
    report = detect_hang()
    assert not report.ok
    assert any(r.startswith("silence_") for r in report.reasons)
    assert report.zero_send_streak >= 6


def test_unload_stuck_inbox_vod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    parked = root / "youtube_nightly" / "parked"
    inbox.mkdir(parents=True)
    parked.mkdir(parents=True)
    mp4 = inbox / "yt_ABCDEF12345.mp4"
    mp4.write_bytes(b"x" * 1000)
    state_path = root / "vod_segment_state.json"
    state_path.write_text(json.dumps({"vods": [{"id": "ABCDEF12345"}]}), encoding="utf-8")
    log = tmp_path / "feed.log"
    log.write_text(
        "\n".join(
            f"2026-09-03 12:{i:02d}:00 pipeline done sent=0 vods=1 game=pubg metro_reject=1 ABCDEF12345"
            for i in range(5)
        ),
        encoding="utf-8",
    )

    class FakeSpec:
        def inbox(self):
            return inbox

        def feed_sent_path(self):
            return root / "sent.json"

    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    monkeypatch.setattr("vod_hang_detector.feed_log_path", lambda: log)
    monkeypatch.setattr("vod_hang_detector.spec", lambda g: FakeSpec())
    monkeypatch.setattr(
        "vod_hang_detector.load_state",
        lambda g: json.loads(state_path.read_text(encoding="utf-8")),
    )

    saved: list = []

    def _save(game: str, state: dict) -> None:
        saved.append(state)
        state_path.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr("vod_hang_detector.save_state", _save)
    name = unload_stuck_inbox_vod("pubg", min_rejects=3)
    assert name == mp4.name
    assert not mp4.exists()
    assert (parked / mp4.name).exists()
