from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_quality_score import score_pubg_window  # noqa: E402


def _base_patches(*, loot: bool = False, author_kill: bool = False):
    return (
        patch("pubg_quality_score._owner_bad", return_value=False),
        patch("pubg_combat_gate._pubg_scan_training_ui", return_value=(False, "")),
        patch(
            "pubg_shooting_gate.pubg_probe_segment",
            return_value={
                "gunfire_density": 0.08,
                "burst_ratio": 8.0,
                "audio_rms": 0.05,
                "center_motion": 0.06,
                "crop_box": None,
            },
        ),
        patch(
            "highlight_scorer.score_panns_audio",
            return_value={
                "panns_gunshot": 0.45,
                "panns_machine_gun": 0.30,
                "panns_explosion": 0.10,
                "panns_speech": 0.05,
                "panns_music": 0.02,
                "panns_gun_max": 0.45,
            },
        ),
        patch("gameplay_gate.segment_looks_like_pubg_loot_or_walk", return_value=loot),
        patch("gameplay_gate.segment_is_valid_for_montage", return_value=(not loot, "loot_walk" if loot else "ok")),
        patch("pubg_combat_gate.pubg_combat_visual_strict", return_value=(True, "ok", {})),
        patch("pubg_killfeed_ocr.score_killfeed_segment", return_value=(0.30, {})),
        patch(
            "shooter_author_kill_gate.author_kill_window_ok",
            return_value=(
                author_kill,
                "author_kill_feed" if author_kill else "no_author_kill",
                {"has_author_kill": author_kill, "author_death": False},
            ),
        ),
        patch("pubg_moment_ranker.predict_from_features", return_value=None),
    )


def test_no_kill_is_penalty_not_hard_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_QUALITY_SCORE_MIN", "0.48")
    patches = _base_patches(author_kill=False)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 100, 14)
    assert ok is True
    assert reason.startswith("quality_ok")
    assert report["penalties"]["no_author_kill"] > 0
    assert "hard_reject" not in report


def test_owner_bad_remains_hard_reject() -> None:
    with patch("pubg_quality_score._owner_bad", return_value=True):
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 100, 14)
    assert ok is False
    assert reason == "hard_owner_bad_window"
    assert report["hard_reject"] == "owner_bad_window"


def test_author_death_without_kill_remains_hard_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    patches = list(_base_patches(author_kill=False))
    patches[8] = patch(
        "shooter_author_kill_gate.author_kill_window_ok",
        return_value=(
            False,
            "author_death_screen",
            {"has_author_kill": False, "author_death": True},
        ),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 100, 14)
    assert ok is False
    assert reason.startswith("hard_author_death")
    assert report["hard_reject"] == "author_death"
