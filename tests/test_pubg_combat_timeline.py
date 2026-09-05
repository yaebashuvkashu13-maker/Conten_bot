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
        assert float(env["DISLIKE_MENU_OVERLAY_MAX"]) == pytest.approx(0.30)
        assert env["PUBG_HARD_REJECT_MENU_OVERLAY"] == "1"
        assert float(env["DISLIKE_GUN_DENSITY_MIN"]) <= 0.015
        assert env["SHOOTER_VOD_DENSE_POOL_BUST"] == "1"
        assert env["PUBG_REJECT_LOOT_WALK"] == "1"
        assert env["PUBG_PRESEND_SHOOTING_GATE"] == "1"


def test_dislike_gun_floor_respects_drought_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from dislike_reason_gates import evaluate_reason_gates

    monkeypatch.setenv("DISLIKE_GUN_DENSITY_MIN", "0.015")
    monkeypatch.setenv("DISLIKE_MENU_OVERLAY_MAX", "0.30")
    ok, reason, report = evaluate_reason_gates(
        {
            "gun_density": 0.086,
            "burst_ratio": 8.0,
            "center_motion": 0.05,
            "menu_overlay": 0.10,
            "visual": 0.6,
        },
        active_reasons=["menu"],
    )
    assert ok is True, reason
    assert report["floors"]["gun_density_min"] <= 0.015


def test_dislike_rejects_junk_menu_at_drought_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """6tBEG4XXXP8_1783 had center_text=0.324 — must die at esc2 menu cap 0.30."""
    from dislike_reason_gates import evaluate_reason_gates

    monkeypatch.setenv("DISLIKE_MENU_OVERLAY_MAX", "0.30")
    monkeypatch.setenv("DISLIKE_GUN_DENSITY_MIN", "0.015")
    ok, reason, _report = evaluate_reason_gates(
        {
            "gun_density": 0.053,
            "burst_ratio": 19.0,
            "center_motion": 0.25,
            "menu_overlay": 0.324,
        },
        active_reasons=["menu"],
    )
    assert ok is False
    assert "menu" in reason


def test_timeline_score_cannot_authorize_send() -> None:
    from pubg_combat_timeline import hard_gates_required, timeline_cannot_authorize_send

    assert timeline_cannot_authorize_send(0.99) is True
    gates = hard_gates_required()
    assert "rendered_mp4_presend" in gates
    assert "early_hook_rendered_0_2s" in gates
    assert "panns_gun_threshold" in gates


def test_cost_limits_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from pubg_combat_timeline import timeline_cost_limits

    monkeypatch.setenv("PUBG_TIMELINE_MAX_ZONES", "15")
    monkeypatch.setenv("PUBG_TIMELINE_MAX_DENSE_SECONDS", "120")
    monkeypatch.setenv("PUBG_TIMELINE_OCR_TOP_N", "8")
    monkeypatch.setenv("PUBG_TIMELINE_RENDER_TOP_N", "1")
    monkeypatch.setenv("PUBG_EARLY_HOOK_MAX_SHIFT_ATTEMPTS", "2")
    limits = timeline_cost_limits()
    assert limits.max_zones == 15
    assert limits.max_dense_seconds == 120.0
    assert limits.ocr_top_n == 8
    assert limits.render_top_n == 1
    assert limits.early_hook_max_shift_attempts == 2


def test_cluster_collapses_near_peaks() -> None:
    from pubg_combat_timeline import (
        TimelinePoint,
        combat_score_point,
        merge_combat_events,
    )

    # One fight sampled every 2s should become a single cluster.
    points = [
        TimelinePoint(t=120.0 + i * 2.0, combat=combat_score_point(gunfire=0.7), gunfire=0.7)
        for i in range(6)
    ]
    # Second fight far away.
    points += [
        TimelinePoint(t=400.0 + i * 2.0, combat=combat_score_point(gunfire=0.75), gunfire=0.75)
        for i in range(4)
    ]
    events = merge_combat_events(points, duration_sec=600.0, merge_gap_sec=6.0)
    assert len(events) == 2
    assert events[0].start <= 122.0
    assert events[0].end >= 130.0
    assert events[0].gunfire_seconds >= 3.0
    assert events[1].peak >= 400.0


def test_early_hook_scores_rendered_not_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pubg_combat_timeline import REASON_EARLY_HOOK_LOW, score_early_hook_on_rendered

    rendered = tmp_path / "clip.mp4"
    rendered.write_bytes(b"fake")

    def fake_hook(path, *, window_sec=None):
        assert path == rendered or Path(path) == rendered
        assert window_sec == 2.0 or window_sec is None or window_sec == 2.0
        return False, "hook_silent:rms=0.01", {"max_rms": 0.01, "y_delta": 0.2, "max_menu": 0.1}

    monkeypatch.setattr("clip_hook_gate.hook_gate_clip", fake_hook, raising=False)
    import clip_hook_gate as chg

    monkeypatch.setattr(chg, "hook_gate_clip", fake_hook)
    score, reason, report = score_early_hook_on_rendered(rendered, window_sec=2.0)
    assert report.get("early_hook_on") == "rendered_mp4"
    assert REASON_EARLY_HOOK_LOW in reason or reason.startswith("hook_")
    assert score >= 0.0


def test_shadow_mode_default_off_enforce() -> None:
    from pubg_combat_timeline import timeline_enforce_enabled

    assert timeline_enforce_enabled() is False
