"""VOD feed recover — locks, pauses, cooldowns, supervisor restart."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_feed_recover import (  # noqa: E402
    bump_scan_cooldowns,
    clear_discovery_pauses,
    clear_feed_locks,
    clear_stale_owner_batch_lock,
    estimate_video_wait_eta,
    park_exhausted_inbox,
    run_recover,
    unpark_ready_vods,
)
from vod_peak_gap import coerce_peak_sec, pool_peak_seconds  # noqa: E402


def test_clear_feed_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = tmp_path / "pubg_vod_segment_feed.lock"
    lock.write_text("1", encoding="utf-8")
    monkeypatch.setattr(
        "vod_feed_recover.feed_lock_paths",
        lambda: [lock],
    )
    removed = clear_feed_locks()
    assert removed == ["pubg_vod_segment_feed.lock"]
    assert not lock.exists()


def test_clear_discovery_pauses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    root.mkdir()
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps({"discovery_pause_until": 9999999999.0, "vods": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    assert clear_discovery_pauses("pubg") is True
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "discovery_pause_until" not in state


def test_bump_scan_cooldowns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "yt_abc123xyz00.mp4").write_bytes(b"x")
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps(
            {
                "vods": [
                    {
                        "id": "abc123xyz00",
                        "last_scan_at": 1.0,
                        "last_scan_blocked": True,
                        "reject_reason": "not_metro",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    n = bump_scan_cooldowns("pubg")
    assert n == 1
    row = json.loads(state_path.read_text(encoding="utf-8"))["vods"][0]
    assert "last_scan_at" not in row
    assert "reject_reason" not in row


def test_clear_stale_owner_batch_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = tmp_path / "OWNER_BATCH_RUNNING"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 7200
    os.utime(lock, (old, old))
    monkeypatch.setattr("vod_feed_recover.OWNER_BATCH_LOCK", lock)
    monkeypatch.setattr("vod_feed_recover.OWNER_BATCH_STALE_SEC", 3600)
    note = clear_stale_owner_batch_lock()
    assert note and "снят" in note
    assert not lock.exists()


def test_park_exhausted_inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    mp4 = inbox / "yt_abc123xyz00.mp4"
    mp4.write_bytes(b"x")
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps({"vods": [{"id": "abc123xyz00", "exhausted": True}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    n = park_exhausted_inbox("pubg")
    assert n == 1
    assert not mp4.exists()
    assert (root / "youtube_nightly" / "parked" / "yt_abc123xyz00.mp4").exists()


def test_coerce_peak_sec_dict_and_float() -> None:
    assert coerce_peak_sec(12.5) == 12.5
    assert coerce_peak_sec({"peak_sec": 99.1}) == 99.1
    assert pool_peak_seconds([{"peak_sec": 1.0}, 2.0, {"bad": 1}]) == [1.0, 2.0]


def test_unpark_reads_dict_peaks_and_ignores_dead_inbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    parked = root / "youtube_nightly" / "parked"
    inbox.mkdir(parents=True)
    parked.mkdir(parents=True)
    # Dead inbox blocker (fully used dict peaks) — must not consume unpark slots.
    dead = inbox / "yt_dead0000000.mp4"
    dead.write_bytes(b"dead")
    good = parked / "yt_good0000001.mp4"
    good.write_bytes(b"x" * 50_000_000)
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps(
            {
                "vods": [
                    {
                        "id": "dead0000000",
                        "exhausted": False,
                        "last_pool_peaks": [{"peak_sec": 100.0}],
                        "last_scan_at": 1.0,
                    },
                    {
                        "id": "good0000001",
                        "exhausted": True,
                        "reject_reason": "fast_montage_need_2_have_0",
                        "last_pool_peaks": [
                            {"peak_sec": 31.9},
                            {"peak_sec": 74.8},
                            {"peak_sec": 178.4},
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    sent_path = root / "vod_segment_feed_sent.json"
    sent_path.write_text(
        json.dumps({"sent": ["dead0000000_100"], "updated_at": "2026-09-04 05:00:00"}),
        encoding="utf-8",
    )
    index_path = root / "vod_segment_index.json"
    index_path.write_text(json.dumps({"segments": []}), encoding="utf-8")
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))

    # Park dead first (as recover does), then unpark.
    assert park_exhausted_inbox("pubg") == 1
    assert not dead.exists()
    n = unpark_ready_vods("pubg", limit=2)
    assert n == 1
    assert (inbox / "yt_good0000001.mp4").exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    good_row = next(r for r in state["vods"] if r["id"] == "good0000001")
    assert good_row["exhausted"] is False


def test_run_recover_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "yt_abc123xyz00.mp4").write_bytes(b"x")
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps(
            {
                "vods": [{"id": "abc123xyz00", "exhausted": True, "reject_reason": "not_metro"}],
                "discovery_pause_until": 9999999999.0,
                "used_youtube_ids": ["old11111111", "old22222222"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    monkeypatch.setenv("VOD_RECOVER_FORCE_SEND", "0")
    msg = run_recover(
        "pubg",
        restart=lambda **_: (True, "test restart"),
        probe=lambda: {"vod_supervisor": True, "daily_cycle": True, "shooter_feed": True, "telegram_bot": True},
    )
    assert "🔧 Восстановление" in msg
    assert "test restart" in msg
    assert "discovery: очищено used YouTube ID" in msg
    assert "⏱" in msg
    assert "ожидайте первое видео" in msg
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["vods"][0]["exhausted"] is False
    assert "discovery_pause_until" not in state
    assert state.get("used_youtube_ids") == []


def test_run_recover_includes_force_send_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "yt_abc123xyz00.mp4").write_bytes(b"x")
    state_path = root / "vod_segment_state.json"
    state_path.write_text(json.dumps({"vods": [{"id": "abc123xyz00"}]}), encoding="utf-8")
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))

    import vod_force_send

    monkeypatch.setattr(
        vod_force_send,
        "force_send_game",
        lambda game, **_: {"game": game, "sent": 1},
    )

    msg = run_recover(
        "pubg",
        restart=lambda **_: (True, "test restart"),
        probe=lambda: {"vod_supervisor": True, "daily_cycle": True, "shooter_feed": True, "telegram_bot": True},
    )
    assert "отправка PUBG: 1" in msg


def test_estimate_eta_pool_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "yt_abc123xyz00.mp4").write_bytes(b"x")
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps(
            {
                "vods": [
                    {
                        "id": "abc123xyz00",
                        "last_pool_peaks": [{"peak_sec": 100.0}, {"peak_sec": 200.0}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    monkeypatch.setattr("vod_feed_recover.feed_process_alive", lambda: True)
    monkeypatch.setattr("vod_feed_recover._log_age_sec", lambda _p: 600.0)
    msg = estimate_video_wait_eta("pubg")
    assert "⏱ PUBG" in msg
    assert "готовыми пиками" in msg


def test_estimate_eta_exhausted_inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "yt_abc123xyz00.mp4").write_bytes(b"x")
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps({"vods": [{"id": "abc123xyz00", "exhausted": True}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    monkeypatch.setattr("vod_feed_recover.feed_process_alive", lambda: True)
    monkeypatch.setattr("vod_feed_recover._log_age_sec", lambda _p: 600.0)
    msg = estimate_video_wait_eta("pubg")
    assert "/reset pubg" in msg


def test_estimate_eta_pubg_only_skips_other_games(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "mlbb"
    data_root.mkdir()
    (data_root / "EU_PUBG_ONLY").write_text("", encoding="utf-8")
    monkeypatch.setenv("MLBB_DATA_ROOT", str(data_root))
    monkeypatch.setattr("vod_feed_recover.feed_process_alive", lambda: True)
    monkeypatch.setattr("vod_feed_recover._log_age_sec", lambda _p: 30.0)

    msg = estimate_video_wait_eta("all")

    assert "⏱ PUBG:" in msg
    assert "MLBB" not in msg
    assert "STANDOFF" not in msg
    assert "GENSHIN" not in msg
    assert "WOT" not in msg
    assert "склейка сейчас в работе" in msg
