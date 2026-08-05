"""Tests for combat-first MLBB moment detection."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mlbb_combat_moment import (  # noqa: E402
    banner_enrich_only,
    fast_combat_probe,
    moment_anchor_mode,
    passes_combat_moment,
    probe_offsets,
    score_combat_moment,
)


def test_moment_anchor_defaults_combat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MLBB_MOMENT_ANCHOR", raising=False)
    assert moment_anchor_mode() == "combat"
    assert banner_enrich_only() is True


def test_probe_offsets_skip_intro() -> None:
    offs = probe_offsets(900.0, skip_intro=300.0)
    assert offs
    assert all(t >= 300 for t in offs)


def test_score_combat_moment_blends_bins_and_hud() -> None:
    analysis = {
        "window_seconds": 2.0,
        "center_motion": np.full(200, 0.7, dtype=np.float32),
        "audio": np.full(200, 0.4, dtype=np.float32),
    }
    vod = Path("/tmp/fake_combat_vod.mp4")
    with patch("mlbb_teamfight_detector.score_teamfight_hud", return_value=(0.8, 0.05, 0.02)):
        score, detail = score_combat_moment(vod, 120.0, analysis=analysis)
    assert score > 0.5
    assert detail["bins"] > 0.4
    assert passes_combat_moment(score)


def test_fast_combat_probe_passes_with_mocked_scores() -> None:
    vod = Path("/tmp/fake_fast_combat.mp4")
    with (
        patch("smart_video_editor.ffprobe_duration", return_value=900.0),
        patch("mlbb_fight_segment._analysis_for", return_value={"window_seconds": 2.0, "center_motion": [], "audio": []}),
        patch(
            "mlbb_combat_moment.score_combat_moment",
            side_effect=[(0.55, {}), (0.2, {}), (0.6, {}), (0.1, {}), (0.5, {}), (0.15, {})],
        ),
    ):
        ok, reason, seeds = fast_combat_probe(vod)
    assert ok is True
    assert seeds
    assert "combat_probe" in reason


def test_resolve_fight_bounds_combat_mode_skips_banner_requirement() -> None:
    import mlbb_kill_banner as kb

    vod = Path("/tmp/fake_combat_bounds.mp4")
    old = {k: os.environ.get(k) for k in ("MLBB_MOMENT_ANCHOR", "MLBB_BANNER_ENRICH_ONLY", "MLBB_VOD_KILL_BANNER")}
    os.environ["MLBB_MOMENT_ANCHOR"] = "combat"
    os.environ["MLBB_BANNER_ENRICH_ONLY"] = "1"
    os.environ["MLBB_VOD_KILL_BANNER"] = "1"
    try:
        with (
            patch.object(kb, "find_banner_near_peak", return_value=None),
            patch("mlbb_fight_segment.detect_fight_bounds", return_value=(90.0, 118.0, 28.0)),
            patch("mlbb_combat_moment.enrich_banner_meta", side_effect=lambda _v, _p, m: m),
        ):
            out = kb.resolve_fight_bounds(vod, 100.0, 600.0)
        assert out is not None
        assert out[3]["anchor"] == "motion"
    finally:
        for key, val in old.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
