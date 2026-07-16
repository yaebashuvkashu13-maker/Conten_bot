"""Regression tests for durable silence → throughput unlock."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import mlbb_vod_throughput_mode as tm  # noqa: E402
from mlbb_vod_adaptive_gate import adaptive_env, soften_level  # noqa: E402


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MLBB_THROUGHPUT_FLAG_PATH", str(tmp_path / "vod_throughput_unlock.json"))
    monkeypatch.setenv("MLBB_THROUGHPUT_SILENCE_SEC", "1800")
    monkeypatch.delenv("MLBB_VOD_THROUGHPUT_MODE", raising=False)
    return tmp_path


def test_unknown_send_age_does_not_force_unlock(data_root):
    assert tm.last_send_age_sec() is None
    assert tm.silence_locked() is False
    assert tm.should_engage() is False


def test_silence_locked_from_sent_json(data_root):
    sent = data_root / "vod_segment_feed_sent.json"
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 4000))
    sent.write_text(json.dumps({"sent_ids": ["x"], "updated_at": old}), encoding="utf-8")
    assert tm.last_send_age_sec() >= 1800
    assert tm.silence_locked() is True
    assert tm.should_engage() is True


def test_apply_and_mark_send_success(data_root, monkeypatch):
    monkeypatch.setenv("MLBB_THROUGHPUT_CLEAR_AFTER_SENDS", "2")
    ov = tm.apply_throughput_mode(reason="test")
    assert ov["MLBB_KILL_BANNER_REQUIRED"] == "0"
    assert os.environ["MLBB_VOD_THROUGHPUT_MODE"] == "1"
    assert tm.flag_active() is True
    tm.mark_send_success()
    # First send keeps unlock armed so the next VOD cannot flip back to strict.
    assert tm.flag_active() is True
    assert os.environ["MLBB_VOD_THROUGHPUT_MODE"] == "1"
    tm.mark_send_success()
    assert tm.flag_active() is False
    assert os.environ.get("MLBB_VOD_THROUGHPUT_MODE") is None


def test_streak_engage_without_silence(data_root, monkeypatch):
    monkeypatch.setenv("MLBB_RELAX_AFTER_ZERO_VODS", "2")
    assert tm.should_engage(adaptive_streak=2) is True
    assert tm.silence_locked() is False


def test_soften_respects_disable_unless_silence(data_root, monkeypatch):
    monkeypatch.setenv("MLBB_VOD_DISABLE_SOFTEN", "1")
    monkeypatch.setenv("MLBB_VOD_ZERO_STREAK_SOFTEN", "3")
    assert soften_level(99) == 0
    tm.write_flag(reason="test", send_age=9999)
    assert soften_level(0) == 2


def test_adaptive_env_does_not_sticky_unlock_on_streak(data_root, monkeypatch):
    monkeypatch.setenv("MLBB_VOD_ZERO_STREAK_SOFTEN", "3")
    monkeypatch.setenv("MLBB_VOD_MIN_CLIP_SCORE", "0.08")
    with adaptive_env(3) as level:
        assert level == 1
        assert os.environ["MLBB_VOD_MIN_CLIP_SCORE"] == "0.04"
    assert os.environ["MLBB_VOD_MIN_CLIP_SCORE"] == "0.08"
    assert tm.flag_active() is False


def test_adaptive_env_holds_when_silence_locked(data_root, monkeypatch):
    monkeypatch.setenv("MLBB_VOD_ZERO_STREAK_SOFTEN", "3")
    monkeypatch.setenv("MLBB_VOD_MIN_CLIP_SCORE", "0.08")
    tm.write_flag(reason="silence", send_age=9999)
    with adaptive_env(0) as level:
        assert level >= 2
        assert os.environ["MLBB_VOD_THROUGHPUT_MODE"] == "1"
    assert tm.flag_active() is True
    assert os.environ["MLBB_KILL_BANNER_REQUIRED"] == "0"


def test_title_gate_suppressed_while_throughput(data_root, monkeypatch):
    """Segment feed must not re-arm TITLE_MIN_TIER under unlock."""
    monkeypatch.setenv("MLBB_KILL_BANNER_REQUIRED", "1")
    tm.apply_throughput_mode(reason="test")
    # Mimic feed title-gate decision.
    title_tier = 5
    throughput = tm.ensure_throughput_env() or tm.is_active()
    if title_tier > 0 and not throughput and os.environ.get("MLBB_KILL_BANNER_REQUIRED", "1") == "1":
        os.environ["MLBB_VOD_TITLE_MIN_TIER"] = str(title_tier)
    else:
        os.environ.pop("MLBB_VOD_TITLE_MIN_TIER", None)
    assert os.environ.get("MLBB_VOD_TITLE_MIN_TIER") in (None, "0")
    assert os.environ["MLBB_KILL_BANNER_REQUIRED"] == "0"


def test_watchdog_send_age_arms_flag(data_root, monkeypatch):
    """Mirror watchdog absolute send-age kill arming logic."""
    sent = data_root / "vod_segment_feed_sent.json"
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 4000))
    sent.write_text(json.dumps({"sent_ids": ["a"], "updated_at": old}), encoding="utf-8")
    age = tm.last_send_age_sec()
    assert age is not None and age >= 1800
    tm.apply_throughput_mode(reason="watchdog_send_age")
    assert tm.flag_active() is True
    payload = json.loads((data_root / "vod_throughput_unlock.json").read_text(encoding="utf-8"))
    assert payload["reason"] == "watchdog_send_age"
