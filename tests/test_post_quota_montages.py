"""Tests for montage dedup + post-quota helpers."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_montage_dedup_peak_and_vod(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MONTAGE_DEDUP_STATE", str(tmp_path / "dedup.json"))
    monkeypatch.setenv("MONTAGE_DEDUP_PEAK_GAP_SEC", "30")

    import montage_dedup as dedup

    assert dedup.used_vods("mlbb") == set()
    assert not dedup.peak_already_used("mlbb", "abc", 100.0)

    dedup.mark_montage_sent(
        "mlbb",
        day="2026-07-22",
        vod_id="abc",
        peaks=[100.0, 250.0],
        montage_id="abc_m100_250",
    )
    assert "abc" in dedup.used_vods("mlbb")
    assert dedup.day_montage_done("mlbb", "2026-07-22")
    assert dedup.peak_already_used("mlbb", "abc", 105.0)  # within gap
    assert not dedup.peak_already_used("mlbb", "abc", 200.0)

    rows = [
        {"peak_start": 100.0},
        {"peak_start": 400.0},
        {"peak_start": 250.0},
    ]
    kept = dedup.filter_rows_exclude_used("mlbb", "abc", rows)
    assert [r["peak_start"] for r in kept] == [400.0]

    fresh = dedup.prefer_fresh_vods(
        "mlbb",
        [Path("yt_abc.mp4"), Path("yt_xyz.mp4")],
        vod_id_fn=lambda p: p.stem[3:] if p.stem.startswith("yt_") else p.stem,
    )
    assert [p.name for p in fresh] == ["yt_xyz.mp4"]


def test_ignore_daily_quota_and_montage_only(monkeypatch) -> None:
    import daily_game_cycle as cycle

    monkeypatch.setenv("DAILY_GAME_CYCLE_ENABLED", "1")
    monkeypatch.delenv("POST_QUOTA_MONTAGE_PASS", raising=False)
    monkeypatch.delenv("MLBB_VOD_IGNORE_DAILY_QUOTA", raising=False)
    monkeypatch.delenv("VOD_IGNORE_DAILY_QUOTA", raising=False)

    assert cycle.ignore_daily_quota("mlbb") is False
    monkeypatch.setenv("POST_QUOTA_MONTAGE_PASS", "1")
    assert cycle.ignore_daily_quota("mlbb") is True
    ok, reason = cycle.can_send_for_game("mlbb", 1)
    assert ok and reason == "ignore_daily_quota"

    monkeypatch.setenv("MONTAGE_ONLY_MODE", "0")
    assert cycle.montage_only_mode() is False
    monkeypatch.setenv("MONTAGE_ONLY_MODE", "1")
    assert cycle.montage_only_mode() is True


def test_post_quota_enabled_defaults(monkeypatch) -> None:
    import post_quota_montages as pqm

    monkeypatch.delenv("POST_QUOTA_MONTAGE", raising=False)
    monkeypatch.setenv("DAILY_GAME_CYCLE_ENABLED", "1")
    assert pqm.post_quota_enabled() is True
    monkeypatch.setenv("POST_QUOTA_MONTAGE", "0")
    assert pqm.post_quota_enabled() is False
    monkeypatch.setenv("MONTAGE_ONLY_MODE", "1")
    assert pqm.montage_only_mode() is True


def test_record_send_skips_when_ignore(tmp_path: Path, monkeypatch) -> None:
    import daily_game_cycle as cycle

    monkeypatch.setenv("DAILY_GAME_CYCLE_STATE", str(tmp_path / "cycle.json"))
    monkeypatch.setenv("DAILY_GAME_CYCLE_ENABLED", "1")
    monkeypatch.setenv("POST_QUOTA_MONTAGE_PASS", "1")
    cycle.record_send("mlbb", 1)
    assert cycle.send_count("mlbb") == 0


def test_clip_run_fraction_detects_sprint_tail(tmp_path: Path, monkeypatch) -> None:
    import numpy as np
    from unittest.mock import patch

    from mlbb_vod_montage import clip_run_fraction

    monkeypatch.setenv("MLBB_RUN_FRAC_CORE_PAD_SEC", "2")

    bins = 40
    win = 1.0
    audio = np.ones(bins, dtype=np.float32) * 0.05
    motion = np.ones(bins, dtype=np.float32) * 0.02
    scene = np.ones(bins, dtype=np.float32) * 0.05
    # Fight core around t=10
    audio[8:13] = 0.4
    scene[8:13] = 0.35
    motion[8:13] = 0.06
    # Sprint tail: high motion, dead combat
    audio[14:30] = 0.02
    scene[14:30] = 0.02
    motion[14:30] = 0.08

    analysis = {
        "window_seconds": win,
        "bins": bins,
        "duration": float(bins),
        "center_motion": motion,
        "audio": audio,
        "scene": scene,
    }

    with patch("mlbb_fight_segment._analysis_for", return_value=analysis):
        frac = clip_run_fraction(tmp_path / "dummy.mp4", 5.0, 30.0, banner_sec=10.0)
    assert frac > 0.35


def test_post_quota_lock_blocks_parallel(tmp_path: Path, monkeypatch) -> None:
    import fcntl
    import post_quota_montages as pqm

    monkeypatch.setenv("POST_QUOTA_MONTAGE", "1")
    monkeypatch.setenv("DAILY_GAME_CYCLE_ENABLED", "1")
    monkeypatch.setenv("MONTAGE_DEDUP_STATE", str(tmp_path / "dedup.json"))
    lock = tmp_path / "post.lock"
    monkeypatch.setenv("POST_QUOTA_MONTAGE_LOCK", str(lock))

    # Hold the lock in this process.
    fh = open(lock, "a+", encoding="utf-8")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        out = pqm.run_once(token="", chat_id="", max_games=1)
        assert out.get("reason") == "locked"
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def test_daily_cycle_calls_post_quota(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".video_bot.env"
    env_file.write_text(
        "TG_BOT_TOKEN=tok\nTG_CHAT_ID=1\nDAILY_GAME_CYCLE_ENABLED=1\n",
        encoding="utf-8",
    )
    state_file = tmp_path / "daily_game_cycle.json"
    monkeypatch.setenv("DAILY_GAME_CYCLE_STATE", str(state_file))
    monkeypatch.setenv("DAILY_GAME_CYCLE_ENABLED", "1")
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "1")
    monkeypatch.setenv("POST_QUOTA_MONTAGE", "1")
    monkeypatch.setenv("MONTAGE_DEDUP_STATE", str(tmp_path / "dedup.json"))

    import daily_game_cycle as cycle
    import daily_cycle_runner as runner

    monkeypatch.setattr(runner, "ENV_PATH", env_file)
    for game in cycle.GAME_ORDER:
        cycle.record_send(game, cycle.quota_for(game))

    called: list[dict] = []

    def fake_run_once(**kwargs):
        called.append(kwargs)
        return {"skipped": True, "reason": "test"}

    sent: list[str] = []

    def fake_send(token: str, chat_id: str, text: str) -> None:
        sent.append(text)

    from unittest.mock import patch

    with patch("mlbb_vod_segment_feed.send_message", fake_send):
        with patch("post_quota_montages.run_once", fake_run_once):
            assert runner.main() == 0

    assert len(called) == 1
    assert any("склейке" in t or "выполнены" in t for t in sent)
