"""Fight-only cuts: no loot/run pad after peak; gun-only coverage; edge trim."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_min_duration_pads_backward_not_loot_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    from pubg_fight_segment import _fit_window_to_gunfire_span

    monkeypatch.setenv("PUBG_SEGMENT_MIN_PAD_BACK_FRAC", "0.75")
    timeline = [{"start": float(t), "gun": 0.08 if 20 <= t <= 26 else 0.0} for t in range(0, 60, 2)]
    gun_active = [float(r["gun"]) >= 0.025 for r in timeline]
    onset = next(i for i, r in enumerate(timeline) if r["start"] == 20)
    start, end = _fit_window_to_gunfire_span(
        timeline,
        gun_active,
        gun_onset=onset,
        start=20.0,
        end=28.0,
        peak_sec=22.0,
        sample=2.0,
        contact_lead=1.0,
        finale_tail=1.0,
        min_duration=16.0,
        max_duration=40.0,
        file_duration=120.0,
    )
    assert start <= 15.0
    assert end <= 34.0


def test_post_peak_default_is_short() -> None:
    src = (SCRIPTS / "pubg_fight_segment.py").read_text(encoding="utf-8")
    assert 'PUBG_SEGMENT_MIN_POST_PEAK_SEC", "3.5"' in src
    assert 'PUBG_SEGMENT_MIN_POST_PEAK_SEC", "10"' not in src


def test_gun_coverage_ignores_loud_score_without_gun() -> None:
    from pubg_clip_shape_gate import _gunfire_coverage, min_gunfire_coverage_frac

    assert min_gunfire_coverage_frac() >= 0.40
    report = {
        "timeline": [
            {"start": 0.0, "gun": 0.0, "score": 0.95},
            {"start": 2.0, "gun": 0.05, "score": 0.1},
            {"start": 4.0, "gun": 0.0, "score": 0.90},
            {"start": 6.0, "gun": 0.06, "score": 0.1},
            {"start": 8.0, "gun": 0.0, "score": 0.85},
        ]
    }
    cov = _gunfire_coverage(report, 0.0, 10.0)
    assert cov is not None
    assert cov == pytest.approx(0.4)


def test_head_tail_run_rejected() -> None:
    from pubg_clip_shape_gate import validate_clip_fight_shape

    report = {
        "shooting_start": 104.0,
        "fight_end": 122.0,
        "timeline": (
            [{"start": float(t), "gun": 0.0, "score": 0.8} for t in range(100, 108, 2)]
            + [{"start": float(t), "gun": 0.09, "score": 0.5} for t in range(108, 118, 2)]
            + [{"start": float(t), "gun": 0.0, "score": 0.8} for t in range(118, 130, 2)]
        ),
    }
    ok, reason = validate_clip_fight_shape(100.0, 28.0, 112.0, report)
    assert not ok
    assert "run" in reason or "loot" in reason or "prefight" in reason


def test_trim_quiet_run_edges_keeps_gun_core() -> None:
    from pubg_montage_bounds import trim_quiet_run_edges

    report = {
        "timeline": (
            [{"start": float(t), "gun": 0.0} for t in range(100, 110, 2)]
            + [{"start": float(t), "gun": 0.08} for t in range(110, 124, 2)]
            + [{"start": float(t), "gun": 0.0} for t in range(124, 140, 2)]
        )
    }
    start, dur = trim_quiet_run_edges(100.0, 40.0, report)
    assert start >= 108.0
    assert start + dur <= 130.0
