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


def test_zero_send_streak_plain_print_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Feed uses print('pipeline done...') without logging timestamps."""
    log = tmp_path / "feed.log"
    log.write_text(
        "pipeline done sent=1 vods=1 game=pubg\n"
        + "\n".join("pipeline done sent=0 vods=0 game=pubg" for _ in range(8))
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("vod_hang_detector.feed_log_path", lambda: log)
    assert zero_send_streak() == 8


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
    monkeypatch.setattr("vod_hang_detector.last_send_age_sec", lambda: 4000.0)
    monkeypatch.setattr("vod_hang_detector.feed_process_alive", lambda: True)
    monkeypatch.setattr("vod_hang_detector.find_stuck_children", lambda *a, **k: [])
    monkeypatch.setattr("vod_hang_detector.find_stuck_part_files", lambda *a, **k: [])
    # No fresh heartbeat → silence counts as hang
    monkeypatch.setattr("vod_hang_detector.read_heartbeat", lambda: {})
    monkeypatch.setattr("vod_hang_detector.inbox_mined_out", lambda *a, **k: False)
    report = detect_hang()
    assert not report.ok
    assert any(r.startswith("silence_") for r in report.reasons)
    assert report.zero_send_streak >= 6


def test_working_feed_not_false_silence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "feed.log"
    old_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 5000))
    log.write_text(f"{old_ts} pipeline done sent=1 vods=1 game=pubg\n", encoding="utf-8")
    monkeypatch.setenv("VOD_SILENCE_WARN_SEC", "3600")
    monkeypatch.setenv("VOD_ABSOLUTE_SILENCE_SEC", "10800")
    monkeypatch.setenv("VOD_PROGRESS_STUCK_SEC", "900")
    monkeypatch.setattr("vod_hang_detector.feed_log_path", lambda: log)
    monkeypatch.setattr("vod_hang_detector.last_send_age_sec", lambda: 5000.0)
    monkeypatch.setattr("vod_hang_detector.feed_process_alive", lambda: True)
    monkeypatch.setattr("vod_hang_detector.find_stuck_children", lambda *a, **k: [])
    monkeypatch.setattr("vod_hang_detector.find_stuck_part_files", lambda *a, **k: [])
    monkeypatch.setattr(
        "vod_hang_detector.read_heartbeat",
        lambda: {"ts": time.time() - 60, "phase": "scan_done"},
    )
    monkeypatch.setattr("vod_hang_detector.inbox_mined_out", lambda *a, **k: False)
    report = detect_hang()
    assert report.ok
    assert not any(r.startswith("silence_") for r in report.reasons)
    assert not any(r.startswith("absolute_silence_") for r in report.reasons)


def test_absolute_silence_heals_despite_fresh_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery spam kept heartbeat fresh for 4h while zero_send_streak was blind."""
    log = tmp_path / "feed.log"
    log.write_text(
        "pipeline done sent=1 vods=1 game=pubg\n"
        + "\n".join("pipeline done sent=0 vods=0 game=pubg" for _ in range(20)),
        encoding="utf-8",
    )
    monkeypatch.setenv("VOD_SILENCE_WARN_SEC", "3600")
    monkeypatch.setenv("VOD_ABSOLUTE_SILENCE_SEC", "7200")
    monkeypatch.setenv("VOD_PROGRESS_STUCK_SEC", "900")
    monkeypatch.setattr("vod_hang_detector.feed_log_path", lambda: log)
    monkeypatch.setattr("vod_hang_detector.last_send_age_sec", lambda: 15000.0)
    monkeypatch.setattr("vod_hang_detector.feed_process_alive", lambda: True)
    monkeypatch.setattr("vod_hang_detector.find_stuck_children", lambda *a, **k: [])
    monkeypatch.setattr("vod_hang_detector.find_stuck_part_files", lambda *a, **k: [])
    monkeypatch.setattr(
        "vod_hang_detector.read_heartbeat",
        lambda: {"ts": time.time() - 30, "phase": "run_start"},
    )
    monkeypatch.setattr("vod_hang_detector.inbox_mined_out", lambda *a, **k: False)
    report = detect_hang()
    assert not report.ok
    assert any(r.startswith("absolute_silence_") for r in report.reasons)
    assert report.zero_send_streak >= 6
    assert any(r.startswith("zero_send_streak_") for r in report.reasons)


def test_fresh_send_ignores_zero_send_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a real send, empty discovery loops must not re-trigger streak heal."""
    log = tmp_path / "feed.log"
    log.write_text(
        "pipeline done sent=1 vods=1 game=pubg\n"
        + "\n".join("pipeline done sent=0 vods=0 game=pubg" for _ in range(20)),
        encoding="utf-8",
    )
    monkeypatch.setenv("VOD_SILENCE_WARN_SEC", "3600")
    monkeypatch.setenv("VOD_ABSOLUTE_SILENCE_SEC", "7200")
    monkeypatch.setattr("vod_hang_detector.feed_log_path", lambda: log)
    monkeypatch.setattr("vod_hang_detector.last_send_age_sec", lambda: 120.0)
    monkeypatch.setattr("vod_hang_detector.feed_process_alive", lambda: True)
    monkeypatch.setattr("vod_hang_detector.find_stuck_children", lambda *a, **k: [])
    monkeypatch.setattr("vod_hang_detector.find_stuck_part_files", lambda *a, **k: [])
    monkeypatch.setattr(
        "vod_hang_detector.read_heartbeat",
        lambda: {"ts": time.time() - 20, "phase": "scan_done"},
    )
    monkeypatch.setattr("vod_hang_detector.inbox_mined_out", lambda *a, **k: False)
    report = detect_hang()
    assert report.ok
    assert not any(r.startswith("zero_send_streak_") for r in report.reasons)


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


def test_mined_inbox_drought_despite_fresh_heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "feed.log"
    log.write_text("2026-09-03 12:00:00 pipeline done sent=1 vods=1 game=pubg\n", encoding="utf-8")
    monkeypatch.setenv("VOD_SILENCE_WARN_SEC", "3600")
    monkeypatch.setenv("VOD_MINED_INBOX_DROUGHT_SEC", "1800")
    monkeypatch.setenv("VOD_PROGRESS_STUCK_SEC", "900")
    monkeypatch.setattr("vod_hang_detector.feed_log_path", lambda: log)
    monkeypatch.setattr("vod_hang_detector.last_send_age_sec", lambda: 2400.0)
    monkeypatch.setattr("vod_hang_detector.feed_process_alive", lambda: True)
    monkeypatch.setattr("vod_hang_detector.find_stuck_children", lambda *a, **k: [])
    monkeypatch.setattr("vod_hang_detector.find_stuck_part_files", lambda *a, **k: [])
    monkeypatch.setattr(
        "vod_hang_detector.read_heartbeat",
        lambda: {"ts": time.time() - 30, "phase": "scanning", "vod": "yt_3hDKNrY4sGU.mp4"},
    )
    monkeypatch.setattr("vod_hang_detector.inbox_mined_out", lambda *a, **k: True)
    report = detect_hang()
    assert not report.ok
    assert any(r.startswith("mined_inbox_drought_") for r in report.reasons)


def test_apply_agent_recover_env_softens_after_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_hang_detector import apply_agent_recover_env

    monkeypatch.setattr("vod_hang_detector.last_send_age_sec", lambda: 4000.0)
    env: dict[str, str] = {}
    out = apply_agent_recover_env(env, escalation=0)
    assert out["VOD_FORCE_SOFTEN"] == "1"
    # Soften thresholds only — never skip discovery (that starved inbox refill).
    assert out["VOD_FORCE_SKIP_DISCOVERY"] == "0"
    assert out["SHOOTER_VOD_SKIP_DISCOVERY"] == "0"
    assert out["VOD_FORCE_PRESEND_BYPASS"] == "0"


def test_apply_agent_recover_env_escalation_lowers_quality(monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_hang_detector import apply_agent_recover_env

    monkeypatch.setattr("vod_hang_detector.last_send_age_sec", lambda: 8000.0)
    env: dict[str, str] = {}
    out = apply_agent_recover_env(env, escalation=2)
    assert out["VOD_FORCE_ESCALATION"] == "2"
    assert float(out["VOD_FORCE_QUALITY_MIN"]) == pytest.approx(0.20)
    assert out["PUBG_PRESEND_SCORE_MODE"] == "1"
    assert out["PUBG_RELAX_OWNER_HEURISTICS"] == "1"
    # Never auto-bypass menu/loot gates under drought escalation.
    assert out["VOD_FORCE_PRESEND_BYPASS"] == "0"
    assert out["VOD_FORCE_SKIP_DISCOVERY"] == "0"
    assert out["SHOOTER_VOD_SKIP_DISCOVERY"] == "0"
    assert out.get("PUBG_PRESEND_SHOOTING_GATE", "1") != "0"


def test_parse_recover_sent() -> None:
    from vod_hang_detector import _parse_recover_sent

    assert _parse_recover_sent("• отправка PUBG: 1 клип(ов) ✅") == 1
    assert _parse_recover_sent("• отправка PUBG: 0 — гейты") == 0


def test_heal_cooldown_retries_when_previous_sent_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vod_hang_detector import _heal_cooldown_ok

    stamp = tmp_path / "heal.json"
    stamp.write_text(
        json.dumps({"last_heal_ts": time.time() - 700, "action": "full_recover", "sent": 0}),
        encoding="utf-8",
    )
    monkeypatch.setattr("vod_hang_detector.DEFAULT_HEAL_STAMP", stamp)
    monkeypatch.setattr("vod_hang_detector.last_send_age_sec", lambda: 5000.0)
    monkeypatch.setenv("VOD_HEAL_RETRY_SEC", "600")
    monkeypatch.setenv("VOD_SILENCE_WARN_SEC", "3600")
    assert _heal_cooldown_ok(2700) is True
