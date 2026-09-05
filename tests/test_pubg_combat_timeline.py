"""Combat timeline: duration-scaled events, no fixed top-3 scene cap."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_adaptive_budget_grows_with_duration() -> None:
    from pubg_combat_timeline import adaptive_candidate_pool, adaptive_event_budget

    short = adaptive_event_budget(10 * 60)
    long = adaptive_event_budget(3 * 3600)
    assert long > short
    assert long >= 30  # 3h must keep far more than a fixed top-3
    assert adaptive_candidate_pool(3 * 3600) >= adaptive_event_budget(3 * 3600)


def test_merge_keeps_tail_events() -> None:
    from pubg_combat_timeline import TimelinePoint, combat_score_point, merge_combat_events

    # Two fights: early + near the end of a long VOD.
    points = [
        TimelinePoint(t=120.0, combat=combat_score_point(gunfire=0.7), gunfire=0.7),
        TimelinePoint(t=122.0, combat=combat_score_point(gunfire=0.8), gunfire=0.8),
        TimelinePoint(t=124.0, combat=combat_score_point(gunfire=0.6), gunfire=0.6),
        TimelinePoint(t=6800.0, combat=combat_score_point(gunfire=0.75), gunfire=0.75),
        TimelinePoint(t=6802.0, combat=combat_score_point(gunfire=0.85), gunfire=0.85),
        TimelinePoint(t=6804.0, combat=combat_score_point(gunfire=0.65), gunfire=0.65),
    ]
    events = merge_combat_events(points, duration_sec=7200.0)
    assert len(events) >= 2
    assert events[0].peak < 200
    assert events[-1].peak > 6000


def test_refine_peaks_not_capped_at_three() -> None:
    from pubg_combat_timeline import refine_peaks_with_timeline

    peaks = [100.0 + i * 120.0 for i in range(12)]  # 12 spaced fights
    out = refine_peaks_with_timeline(peaks, duration_sec=2 * 3600)
    assert len(out) >= 6
    assert out[-1] >= peaks[-2]  # tail retained


def test_early_action_prefers_hotter_shift() -> None:
    from pubg_combat_timeline import pick_early_action_start

    start = 100.0
    scores = {100.0: 0.05, 101.0: 0.12, 102.0: 0.55, 103.0: 0.40}
    chosen, score, reason = pick_early_action_start(start, scores)
    assert chosen == 102.0
    assert score == pytest.approx(0.55)
    assert "shift" in reason


def test_burst_cluster_gate() -> None:
    from pubg_combat_timeline import burst_cluster_ok

    ok, _ = burst_cluster_ok(clusters=1, quarters_active=1, active_sec=1.0)
    assert ok is False
    ok, reason = burst_cluster_ok(clusters=3, quarters_active=2, active_sec=3.5)
    assert ok is True
    assert reason == "burst_ok"


def test_drought_sets_timeline_and_dislike_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_force_send import apply_drought_pubg_env
    from vod_hang_detector import apply_agent_recover_env

    monkeypatch.setattr("vod_hang_detector.last_send_age_sec", lambda: 9000.0)
    force = apply_drought_pubg_env({}, escalation=2)
    hang = apply_agent_recover_env({}, escalation=2)
    for env in (force, hang):
        assert env["PUBG_COMBAT_TIMELINE"] == "1"
        assert env["PUBG_EARLY_ACTION_SHIFT"] == "1"
        assert float(env["DISLIKE_MENU_OVERLAY_MAX"]) >= 0.36
        assert env["PUBG_REJECT_LOOT_WALK"] == "1"
        assert env["PUBG_PRESEND_SHOOTING_GATE"] == "1"
