"""Soft L2+ must not override hard classic Metro rejects."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import shooter_vod_segment_feed as feed  # noqa: E402


def test_hard_classic_outdoor_not_softened(monkeypatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_ZERO_STREAK_SOFTEN", "2")

    with patch(
        "pubg_metro_royale_gate.vod_looks_metro_royale",
        return_value=(
            False,
            "metro_vod_reject=0/3 (180s:classic_outdoor_sky=1/1;72s:classic_outdoor_sky=3/3)",
        ),
    ):
        ok, reason = feed._pubg_metro_vod_ok(
            Path("yt_classic01.mp4"),
            title="Solo vs squads Metro Royale clutch fight",
            streak=7,
        )
    assert ok is False
    assert "classic_outdoor_sky" in reason


def test_training_title_rejected_before_frames() -> None:
    ok, reason = feed._pubg_metro_vod_ok(
        Path("yt_x.mp4"),
        title="Training sniper - pubg metro royal",
        streak=0,
    )
    assert ok is False
    assert reason == "metro_training_junk_title"


def test_classic_reject_always_exhausts() -> None:
    assert (
        feed._pubg_metro_should_exhaust(
            "Metro Royale clutch",
            streak=10,
            reason="metro_vod_reject=0/3 (classic_outdoor_sky=3/3)",
        )
        is True
    )
