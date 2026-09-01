from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_quality_score import score_pubg_window  # noqa: E402


def _base_patches(*, loot: bool = False, author_kill: bool = False):
    motion = 0.035 if loot else 0.06
    gun = 0.025 if loot else 0.08
    return (
        patch("pubg_quality_score._owner_bad", return_value=False),
        patch(
            "pubg_shooting_gate.pubg_probe_segment",
            return_value={
                "gunfire_density": gun,
                "burst_ratio": 8.0,
                "audio_rms": 0.05,
                "center_motion": motion,
                "center_text": 0.0,
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
        patch(
            "pubg_combat_gate.pubg_combat_visual_strict",
            return_value=(
                True,
                "ok",
                {
                    "best_hit_flash": 0.005 if author_kill else 0.0,
                    "best_weapon_edge": 0.04 if author_kill else 0.0,
                },
            ),
        ),
        patch(
            "pubg_killfeed_ocr.score_killfeed_segment",
            return_value=(0.30 if author_kill else 0.0, {}),
        ),
        patch("shooter_author_kill_gate.detect_author_death_signals", return_value=(False, "", {})),
        patch("pubg_combat_gate._pubg_scan_training_ui", return_value=(False, "")),
        patch("pubg_moment_ranker.predict_from_features", return_value=None),
    )


def test_no_kill_fails_payoff_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_QUALITY_SCORE_MIN", "0.48")
    monkeypatch.setenv("PUBG_KILL_NOTIFICATION_MODE", "off")
    patches = _base_patches(author_kill=False)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 100, 14)
    assert ok is False
    assert reason.startswith("payoff_low")
    assert report["payoff_score"] < report["payoff_threshold"]


def test_kill_passes_fight_and_payoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_QUALITY_SCORE_MIN", "0.48")
    monkeypatch.setenv("PUBG_KILL_NOTIFICATION_MODE", "off")
    patches = _base_patches(author_kill=True)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 100, 14)
    assert ok is True
    assert reason.startswith("quality_ok")
    assert report["fight_score"] >= report["fight_threshold"]
    assert report["payoff_score"] >= report["payoff_threshold"]


def test_owner_bad_remains_hard_reject() -> None:
    with patch("pubg_quality_score._owner_bad", return_value=True):
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 100, 14)
    assert ok is False
    assert reason == "hard_owner_bad_window"
    assert report["hard_reject"] == "owner_bad_window"


def test_author_death_without_kill_remains_hard_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    patches = list(_base_patches(author_kill=False))
    patches[5] = patch(
        "shooter_author_kill_gate.detect_author_death_signals",
        return_value=(True, "author_death_screen", {}),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 100, 14)
    assert ok is False
    assert reason.startswith("hard_author_death")
    assert report["hard_reject"] == "author_death"


def test_required_kill_notification_rejects_shooting_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUBG_REQUIRE_KILL_NOTIFICATION", "1")
    patches = _base_patches(author_kill=True)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 100, 14)
    assert ok is False
    assert reason.startswith("kill_notification_missing")
    assert report["kill_notification_hit"] is False
