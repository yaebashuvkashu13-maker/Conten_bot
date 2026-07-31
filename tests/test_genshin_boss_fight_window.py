"""Tests for Genshin boss-fight window expansion."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import genshin_boss_fight_window as gfw  # noqa: E402


def test_expand_walks_back_to_onset(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GENSHIN_BOSS_FULL_FIGHT", "1")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_BAR_STEP_SEC", "2")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_BAR_KEEP", "0.10")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_MIN_SEC", "28")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_MAX_SEC", "90")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_HARD_MAX_SEC", "120")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_MAX_BACK_SEC", "70")
    monkeypatch.setenv("GENSHIN_VOD_LEAD_SEC", "5")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_POST_SEC", "4")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_GAP_TOLERATE", "1")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_PREFER_START", "1")

    # Fake bar: appears at t=100, stays until 180; peak at 160 (mid/half HP zone).
    def fake_bar(_path, t, _cap):
        t = float(t)
        if 100 <= t <= 180:
            return 0.55 if t < 140 else 0.25
        return 0.0

    fake = tmp_path / "vod.mp4"
    fake.write_bytes(b"x")
    cap = MagicMock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda prop: {5: 30.0, 7: 30 * 300}.get(int(prop), 0.0)

    with patch.object(gfw, "_bar_at_cap", side_effect=fake_bar), patch(
        "cv2.VideoCapture", return_value=cap
    ):
        start, dur, meta = gfw.expand_boss_fight_window(fake, 160.0, vod_duration=300.0)

    assert meta["enabled"] is True
    assert start <= 105.0
    assert start >= 90.0
    assert dur >= 28.0
    assert meta["onset"] <= 105.0
    assert start <= 160.0 <= start + dur


def test_prefer_start_never_drops_peak(monkeypatch, tmp_path: Path) -> None:
    """Regression: early false bar + prefer_start used to end before the peak."""
    monkeypatch.setenv("GENSHIN_BOSS_FULL_FIGHT", "1")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_BAR_STEP_SEC", "2")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_BAR_KEEP", "0.10")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_MIN_SEC", "28")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_MAX_SEC", "90")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_HARD_MAX_SEC", "120")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_MAX_BACK_SEC", "45")
    monkeypatch.setenv("GENSHIN_VOD_LEAD_SEC", "5")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_POST_SEC", "10")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_GAP_TOLERATE", "2")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_PREFER_START", "1")

    # Bar from 10..200 (false early) with peak at 129 — old logic started ~6 and
    # truncated at 96, dropping the peak.
    def fake_bar(_path, t, _cap):
        t = float(t)
        return 0.4 if 10 <= t <= 200 else 0.0

    fake = tmp_path / "vod.mp4"
    fake.write_bytes(b"x")
    cap = MagicMock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda prop: {5: 30.0, 7: 30 * 400}.get(int(prop), 0.0)

    with patch.object(gfw, "_bar_at_cap", side_effect=fake_bar), patch(
        "cv2.VideoCapture", return_value=cap
    ):
        start, dur, meta = gfw.expand_boss_fight_window(fake, 129.0, vod_duration=400.0)

    assert start <= 129.0 <= start + dur
    # Max-back must stop cutscene grab near t=0.
    assert start >= 129.0 - 45.0 - 5.0 - 1.0
    assert (start + dur) >= 129.0 + 8.0


def test_default_keeps_finish_trims_early_cutscene(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GENSHIN_BOSS_FULL_FIGHT", "1")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_BAR_STEP_SEC", "2")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_BAR_KEEP", "0.10")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_MIN_SEC", "28")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_MAX_SEC", "90")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_HARD_MAX_SEC", "140")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_MAX_BACK_SEC", "45")
    monkeypatch.setenv("GENSHIN_VOD_LEAD_SEC", "3")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_POST_SEC", "10")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_GAP_TOLERATE", "1")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_PREFER_START", "0")

    def fake_bar(_path, t, _cap):
        t = float(t)
        return 0.35 if 80 <= t <= 170 else 0.0

    fake = tmp_path / "vod.mp4"
    fake.write_bytes(b"x")
    cap = MagicMock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda prop: {5: 30.0, 7: 30 * 300}.get(int(prop), 0.0)

    with patch.object(gfw, "_bar_at_cap", side_effect=fake_bar), patch(
        "cv2.VideoCapture", return_value=cap
    ):
        start, dur, meta = gfw.expand_boss_fight_window(fake, 140.0, vod_duration=300.0)

    assert start <= 140.0 <= start + dur
    assert (start + dur) >= 150.0  # post-roll after peak
    assert start >= 70.0  # not the far early false onset


def test_disabled_falls_back_to_short_lead(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GENSHIN_BOSS_FULL_FIGHT", "0")
    monkeypatch.setenv("GENSHIN_VOD_LEAD_SEC", "5")
    monkeypatch.setenv("HIGHLIGHT_WINDOW_SEC", "15")
    fake = tmp_path / "vod.mp4"
    fake.write_bytes(b"x")
    start, dur, meta = gfw.expand_boss_fight_window(fake, 200.0)
    assert meta["enabled"] is False
    assert start == 195.0
    assert dur == 15.0
