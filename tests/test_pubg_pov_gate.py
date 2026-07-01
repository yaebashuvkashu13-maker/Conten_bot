"""Tests for PUBG POV engagement gate."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_combat_gate import pubg_pov_engagement_ok  # noqa: E402


def test_rejects_background_gunfire() -> None:
    with patch("pubg_combat_gate._gunfire_pvp_shape", return_value=(1, 1, 0.1)):
        ok, reason, _ = pubg_pov_engagement_ok(
            Path("x.mp4"),
            100.0,
            12.0,
            gunfire_density=0.09,
            center_motion=0.01,
        )
    assert ok is False
    assert "background_gunfire" in reason


def test_passes_pov_engagement() -> None:
    with patch("pubg_combat_gate._gunfire_pvp_shape", return_value=(3, 3, 0.6)):
        ok, reason, _ = pubg_pov_engagement_ok(
            Path("x.mp4"),
            100.0,
            12.0,
            gunfire_density=0.09,
            center_motion=0.05,
        )
    assert ok is True
    assert reason == "pov_engagement_ok"
