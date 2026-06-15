"""Tests for mlbb_hud_signals."""

from __future__ import annotations

from mlbb_hud_signals import HudSignals, _estimate_replay_likelihood, hud_learning_boost


def test_replay_title_increases_likelihood() -> None:
    score = _estimate_replay_likelihood(
        title="MLBB ranked replay savage",
        center_motion=0.03,
        joystick_activity=0.002,
        minimap_activity=0.01,
        top_hud_activity=0.005,
    )
    assert score >= 0.5


def test_active_joystick_reduces_replay_likelihood() -> None:
    score = _estimate_replay_likelihood(
        title="mlbb savage gameplay",
        center_motion=0.025,
        joystick_activity=0.02,
        minimap_activity=0.012,
        top_hud_activity=0.008,
    )
    assert score < 0.35


def test_hud_learning_boost_prefers_live() -> None:
    live = HudSignals(
        combat_intensity=0.7,
        live_match_likelihood=0.8,
        replay_likelihood=0.1,
    )
    replay = HudSignals(
        combat_intensity=0.7,
        live_match_likelihood=0.1,
        replay_likelihood=0.85,
    )
    assert hud_learning_boost(live) > hud_learning_boost(replay)
