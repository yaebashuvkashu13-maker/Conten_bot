#!/usr/bin/env python3
"""Tests for WoT brawl fight window expansion."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clear_wot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.startswith("WOT_BRAWL_") or k in {"WOT_VOD_LEAD_SEC", "SHOOTER_VOD_VARIABLE_LENGTH"}:
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WOT_BRAWL_FULL_FIGHT", "1")
    monkeypatch.setenv("WOT_BRAWL_EXPAND_MODE", "peak")
    monkeypatch.setenv("WOT_VOD_LEAD_SEC", "8")
    monkeypatch.setenv("WOT_BRAWL_PEAK_TAIL_SEC", "20")
    monkeypatch.setenv("WOT_BRAWL_FIGHT_MAX_SEC", "42")
    monkeypatch.setenv("WOT_BRAWL_FIGHT_HARD_MAX_SEC", "60")
    monkeypatch.setenv("WOT_BRAWL_FIGHT_MIN_SEC", "18")


def test_peak_mode_keeps_run_end_past_fixed_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner: clip ending at peak+tail while flashes continue is too early."""
    from wot_brawl_fight import detect_brawl_bounds

    monkeypatch.setenv("WOT_BRAWL_EXPAND_MODE", "peak")
    series = [(float(t), 0.02 if 250 <= t <= 290 else 0.0, 0.0) for t in range(220, 320)]
    with (
        patch("wot_brawl_fight._combat_series", return_value=series),
        patch("smart_video_editor.ffprobe_duration", return_value=400.0),
    ):
        start, end, dur = detect_brawl_bounds(Path("/tmp/fake.mp4"), 261.0)
    assert start == pytest.approx(253.0, abs=0.5)
    # Must reach past the old peak+10 / 15s window into the continuing exchange.
    assert end >= 290.0
    assert dur >= 35.0
    assert start <= 261.0 <= end


def test_fallback_uses_longer_tail_when_no_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from wot_brawl_fight import detect_brawl_bounds

    with (
        patch("wot_brawl_fight._combat_series", return_value=[]),
        patch("wot_brawl_fight._run_containing_peak", return_value=None),
        patch("smart_video_editor.ffprobe_duration", return_value=400.0),
    ):
        start, end, dur = detect_brawl_bounds(Path("/tmp/fake.mp4"), 261.0)
    assert start == pytest.approx(253.0, abs=0.1)
    assert end >= 281.0  # peak + 20 tail
    assert dur >= 18.0


def test_expand_disabled_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    from wot_brawl_fight import expand_clip_to_full_brawl

    monkeypatch.setenv("WOT_BRAWL_FULL_FIGHT", "0")
    clip = {"start": 253.0, "peak_start": 261.0, "input_duration": 15.0}
    assert expand_clip_to_full_brawl(Path("/tmp/fake.mp4"), clip) is clip
