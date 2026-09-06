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
    shoot_ok = not loot
    return (
        patch("pubg_quality_score._owner_bad", return_value=False),
        patch(
            "pubg_shooting_gate.pubg_passes_shooting_gate",
            return_value=(shoot_ok, "strict_gun" if shoot_ok else "loot_walk", {}),
        ),
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
            return_value=(
                0.55 if author_kill else 0.0,
                {
                    "notification_score": 0.62 if author_kill else 0.0,
                    "notification_hit": author_kill,
                    "killfeed_hits": ["kill"] if author_kill else [],
                },
            ),
        ),
        patch("shooter_author_kill_gate.detect_author_death_signals", return_value=(False, "", {})),
        patch("pubg_combat_gate._pubg_scan_training_ui", return_value=(False, "")),
        patch("pubg_moment_ranker.predict_from_features", return_value=None),
    )


def test_no_kill_fails_early_payoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_EARLY_PAYOFF_REJECT", "1")
    patches = _base_patches(author_kill=False)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 100, 14, use_cache=False)
    assert ok is False
    assert reason.startswith("early_payoff_low")


def test_no_kill_fails_payoff_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_EARLY_PAYOFF_REJECT", "0")
    monkeypatch.setenv("PUBG_QUALITY_SCORE_MIN", "0.48")
    patches = _base_patches(author_kill=False)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 100, 14, use_cache=False)
    assert ok is False
    assert reason.startswith("payoff_low")


def test_kill_passes_fight_and_payoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_QUALITY_SCORE_MIN", "0.48")
    patches = _base_patches(author_kill=True)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 100, 14, use_cache=False)
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
    monkeypatch.setenv("PUBG_EARLY_PAYOFF_REJECT", "0")
    patches = list(_base_patches(author_kill=False))
    patches[6] = patch(
        "shooter_author_kill_gate.detect_author_death_signals",
        return_value=(True, "author_death_screen", {}),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 100, 14)
    assert ok is False
    assert reason.startswith("hard_author_death")
    assert report["hard_reject"] == "author_death"


def test_required_kill_notification_rejects_shooting_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUBG_REQUIRE_KILL_NOTIFICATION", "1")
    monkeypatch.setenv("PUBG_EARLY_PAYOFF_REJECT", "0")
    patches = _base_patches(author_kill=False)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 100, 14)
    assert ok is False
    assert reason.startswith("kill_notification_missing")
    assert report["kill_notification_hit"] is False


def test_notification_without_gunfire_not_treated_as_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """False OCR kill banner (ACCvn55IvVw) must not pass as author kill."""
    monkeypatch.setenv("PUBG_EARLY_PAYOFF_REJECT", "0")
    monkeypatch.setenv("PUBG_REJECT_LOOT_WALK", "0")
    monkeypatch.setenv("PUBG_QUALITY_SCORE_MIN", "0.48")
    patches = list(_base_patches(author_kill=False))
    patches[1] = patch(
        "pubg_shooting_gate.pubg_passes_shooting_gate",
        return_value=(False, "no_shots", {}),
    )
    patches[2] = patch(
        "pubg_shooting_gate.pubg_probe_segment",
        return_value={
            "gunfire_density": 0.025,
            "burst_ratio": 1.0,
            "audio_rms": 0.02,
            "center_motion": 0.06,
            "center_text": 0.0,
            "crop_box": None,
        },
    )
    patches[5] = patch(
        "pubg_killfeed_ocr.score_killfeed_segment",
        return_value=(
            0.0,
            {
                "notification_score": 0.59,
                "notification_hit": True,
                "killfeed_hits": [],
            },
        ),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 123, 11, use_cache=False)
    assert ok is False
    assert report.get("hard_reject") == "no_shots"


def test_menu_overlay_on_any_frame_hard_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inventory UI on start must hard-reject even if mid/end look like combat."""
    monkeypatch.setenv("PUBG_HARD_REJECT_MENU_OVERLAY", "1")
    monkeypatch.setenv("PUBG_EARLY_PAYOFF_REJECT", "0")
    # Adaptive tests may leak SMART_PUBG_MIN_GUNFIRE_DENSITY into os.environ;
    # isolate so missing-video combat scoring cannot preempt menu_overlay.
    monkeypatch.delenv("SMART_PUBG_MIN_GUNFIRE_DENSITY", raising=False)
    monkeypatch.setenv("PUBG_REJECT_LOOT_WALK", "0")
    patches = list(_base_patches(author_kill=True))
    # Weak gun/PANNs = real inventory/lobby, not an ADS false positive.
    patches[2] = patch(
        "pubg_shooting_gate.pubg_probe_segment",
        return_value={
            "gunfire_density": 0.05,
            "burst_ratio": 3.0,
            "audio_rms": 0.03,
            "center_motion": 0.05,
            "center_text": 0.0,
            "crop_box": None,
        },
    )
    patches[3] = patch(
        "highlight_scorer.score_panns_audio",
        return_value={
            "panns_gunshot": 0.08,
            "panns_machine_gun": 0.05,
            "panns_explosion": 0.01,
            "panns_speech": 0.05,
            "panns_music": 0.02,
            "panns_gun_max": 0.08,
        },
    )
    patches[4] = patch(
        "pubg_combat_gate.pubg_combat_visual_strict",
        return_value=(
            True,
            "combat_visual_strict",
            {
                "best_hit_flash": 0.0,
                "best_weapon_edge": 0.04,
                "frames": [
                    {"label": "start", "pass": False, "reason": "menu_overlay"},
                    {"label": "mid", "pass": True, "reason": "combat_visible"},
                    {"label": "end", "pass": True, "reason": "combat_visible"},
                ],
            },
        ),
    )
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patch("gameplay_gate.segment_looks_like_pubg_loot_or_walk", return_value=False),
    ):
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 2538, 24, single=True, use_cache=False)
    assert ok is False
    assert "menu_overlay" in reason
    assert report.get("hard_reject") == "menu_overlay"


def test_menu_overlay_rescued_by_strong_gun_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADS/HUD false menu_overlay must not block a strong-gun singles fight."""
    monkeypatch.setenv("PUBG_HARD_REJECT_MENU_OVERLAY", "1")
    monkeypatch.setenv("PUBG_EARLY_PAYOFF_REJECT", "0")
    patches = list(_base_patches(author_kill=True))
    patches[4] = patch(
        "pubg_combat_gate.pubg_combat_visual_strict",
        return_value=(
            True,
            "combat_visual_strict",
            {
                "best_hit_flash": 0.01,
                "best_weapon_edge": 0.05,
                "frames": [
                    {"label": "start", "pass": False, "reason": "menu_overlay"},
                    {"label": "mid", "pass": False, "reason": "menu_overlay"},
                    {"label": "end", "pass": True, "reason": "combat_visible"},
                ],
            },
        ),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 471.5, 23, single=True, use_cache=False)
    assert report.get("singles_menu_gun_rescue") is True
    assert report.get("hard_reject") != "menu_overlay"


def test_hud_fp_notification_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_HARD_REJECT_MENU_OVERLAY", "0")
    monkeypatch.setenv("PUBG_EARLY_PAYOFF_REJECT", "0")
    patches = list(_base_patches(author_kill=False))
    patches[5] = patch(
        "pubg_killfeed_ocr.score_killfeed_segment",
        return_value=(
            0.60,
            {
                "notification_score": 0.60,
                "notification_hit": True,
                "notification_class": "hud_fp",
                "killfeed_hits": [],
            },
        ),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
        _ok, _reason, report = score_pubg_window(Path("vod.mp4"), 100, 14, single=True, use_cache=False)
    assert report.get("kill_notification_hit") is False
    assert report.get("kill_notification_class") == "hud_fp"


def test_strong_gun_without_kill_still_payoff_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADS spray with strong audio but no kill must NOT bypass payoff_low."""
    monkeypatch.setenv("PUBG_HARD_REJECT_MENU_OVERLAY", "0")
    monkeypatch.setenv("PUBG_EARLY_PAYOFF_REJECT", "0")
    monkeypatch.setenv("PUBG_EARLY_PAYOFF_REJECT_SINGLES", "0")
    monkeypatch.setenv("VOD_FORCE_SOFTEN", "1")
    monkeypatch.setenv("PUBG_SINGLES_GUN_PAYOFF_BYPASS", "1")
    monkeypatch.setenv("PUBG_PAYOFF_SCORE_MIN_SINGLES", "0.16")
    patches = list(_base_patches(author_kill=False))
    patches[2] = patch(
        "pubg_shooting_gate.pubg_probe_segment",
        return_value={
            "gunfire_density": 0.068,
            "burst_ratio": 4.5,
            "audio_rms": 0.04,
            "center_motion": 0.05,
            "center_text": 0.0,
            "crop_box": None,
        },
    )
    patches[3] = patch(
        "highlight_scorer.score_panns_audio",
        return_value={
            "panns_gunshot": 0.55,
            "panns_machine_gun": 0.69,
            "panns_explosion": 0.01,
            "panns_speech": 0.2,
            "panns_music": 0.1,
            "panns_gun_max": 0.69,
        },
    )
    patches[5] = patch(
        "pubg_killfeed_ocr.score_killfeed_segment",
        return_value=(
            0.28,
            {
                "notification_score": 0.22,
                "notification_hit": False,
                "notification_class": "hud_fp",
                "killfeed_hits": [],
            },
        ),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
        ok, reason, report = score_pubg_window(Path("vod.mp4"), 461.5, 22, single=True, use_cache=False)
    assert ok is False
    assert "payoff_low" in reason
    assert report.get("singles_gun_payoff_bypass") is not True
    assert not report.get("has_author_kill")

