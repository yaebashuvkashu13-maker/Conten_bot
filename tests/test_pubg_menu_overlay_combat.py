"""Shooter HUD must not die as menu_overlay when combat edges are present."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from visual_action_check import check_frame_visual  # noqa: E402


def test_pubg_combat_beats_menu_overlay(monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_MENU_OVERLAY_MAX", "0.20")
    monkeypatch.setenv("VISUAL_PUBG_MIN_CENTER_EDGE", "0.01")
    monkeypatch.setenv("VISUAL_PUBG_MIN_WEAPON_EDGE", "0.01")
    monkeypatch.setenv("VISUAL_PUBG_MIN_HIT_FLASH", "0.0")
    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    with patch("visual_action_check._frame_menu_overlay", return_value=0.90), patch(
        "visual_action_check._laplacian_edge_score", side_effect=[0.05, 0.04, 0.01, 0.05]
    ), patch("visual_action_check._frame_hit_flash_score", return_value=0.01), patch(
        "visual_action_check._frame_hud_metrics", return_value=(10.0, 10.0, 5.0)
    ):
        ok, reason, _ = check_frame_visual("pubg", frame)
    assert ok is True, reason
    assert reason == "combat_visible"
