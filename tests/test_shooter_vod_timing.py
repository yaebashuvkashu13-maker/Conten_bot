"""PUBG clip timing offset."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from shooter_vod_timing import peak_lag_sec, window_times  # noqa: E402


def test_pubg_peak_lag_default_zero(monkeypatch) -> None:
    monkeypatch.delenv("SHOOTER_VOD_PEAK_LAG_SEC", raising=False)
    monkeypatch.setenv("PUBG_VOD_PEAK_LAG_SEC", "0")
    assert peak_lag_sec("pubg") == 0.0


def test_bmnfbWwOyg_start_without_global_lag(monkeypatch) -> None:
    """No +10s shift — start stays peak-lead unless owner label says otherwise."""
    monkeypatch.setenv("PUBG_VOD_PEAK_LAG_SEC", "0")
    monkeypatch.setenv("MLBB_VOD_LEAD_SEC", "4")
    start, peak_eff, _ = window_times("pubg", 84.0)
    assert start == 80.0
    assert peak_eff == 84.0
