"""Minimal Shorts montage — tail trim and fade filters."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import mlbb_shorts_montage as montage


def test_compute_send_duration_tail_trim(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_SHORTS_TRIM_TAIL", "1")
    monkeypatch.setenv("MLBB_SHORTS_SEND_MAX_SEC", "28")
    monkeypatch.setenv("MLBB_SHORTS_SEND_MIN_SEC", "8")
    path = Path("/tmp/fake.mp4")
    timeline = [(0.0, 0.01), (5.0, 0.05), (12.0, 0.04), (18.0, 0.02), (25.0, 0.01)]
    with patch.object(montage, "_ffprobe_duration", return_value=45.0):
        out_dur, reason = montage.compute_send_duration(path, 3.0, timeline=timeline)
    assert reason == "tail_trim"
    assert 8.0 <= out_dur <= 28.0
    assert out_dur < 42.0


def test_compute_send_duration_cap_only(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_SHORTS_TRIM_TAIL", "0")
    monkeypatch.setenv("MLBB_SHORTS_SEND_MAX_SEC", "20")
    path = Path("/tmp/fake.mp4")
    with patch.object(montage, "_ffprobe_duration", return_value=60.0):
        out_dur, reason = montage.compute_send_duration(path, 5.0)
    assert reason == "cap_only"
    assert out_dur == 20.0


def test_build_ffmpeg_filters_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_SHORTS_MINI_MONTAGE", "1")
    vf, af = montage.build_ffmpeg_filters(15.0)
    assert "fade=t=in" in vf
    assert "fade=t=out" in vf
    assert "afade=t=in" in af


def test_build_ffmpeg_filters_disabled(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_SHORTS_MINI_MONTAGE", "0")
    vf, af = montage.build_ffmpeg_filters(15.0)
    assert vf == ""
    assert af == ""


def test_build_ffmpeg_filters_skip_short_clip(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_SHORTS_MINI_MONTAGE", "1")
    vf, af = montage.build_ffmpeg_filters(1.0)
    assert vf == ""
    assert af == ""
