"""PUBG clip timing offset."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from shooter_vod_timing import peak_lag_sec, window_times  # noqa: E402


def test_pubg_peak_lag_default_10s(monkeypatch) -> None:
    monkeypatch.delenv("SHOOTER_VOD_PEAK_LAG_SEC", raising=False)
    monkeypatch.setenv("PUBG_VOD_PEAK_LAG_SEC", "10")
    monkeypatch.setenv("MLBB_VOD_LEAD_SEC", "4")
    assert peak_lag_sec("pubg") == 10.0
    start, peak_eff, dur = window_times("pubg", 84.0)
    assert start == 90.0
    assert peak_eff == 94.0
    assert dur == 10.0


def test_bmnfbWwOyg_user_feedback_window(monkeypatch) -> None:
    """Owner: _bmnfbWwOyg @ 80s peak 84s → want start 90 peak 94."""
    monkeypatch.setenv("PUBG_VOD_PEAK_LAG_SEC", "10")
    monkeypatch.setenv("MLBB_VOD_LEAD_SEC", "4")
    start, peak_eff, _ = window_times("pubg", 84.0)
    assert start == 90.0
    assert peak_eff == 94.0
