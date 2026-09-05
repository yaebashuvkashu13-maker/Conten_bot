"""Payoff drought guards: singles OCR-miss must not hard-block when gun is real."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_payoff_defaults_rescue_singles_ocr_miss() -> None:
    src = (SCRIPTS / "pubg_quality_score.py").read_text(encoding="utf-8")
    assert 'PUBG_FAST_PAYOFF_MIN", "0.12"' in src
    assert 'PUBG_EARLY_PAYOFF_REJECT_SINGLES", "0"' in src
    assert 'PUBG_SINGLES_GUN_PAYOFF_BYPASS", "1"' in src
    assert 'PUBG_PAYOFF_SCORE_MIN", "0.28"' in src
    assert 'PUBG_PAYOFF_SCORE_MIN_SINGLES", "0.10"' in src
    assert "singles_gun_early_payoff_rescue" in src


def test_feedback_bridge_moves_floors(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VOD_QUALITY_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("VOD_ADAPTIVE_THRESH_DIR", str(tmp_path / "adaptive"))
    from game_adaptive_thresholds import thresholds_for
    from vod_owner_feedback_bridge import apply_owner_feedback

    before = thresholds_for("pubg")["gun_density_min"]
    for i in range(5):
        apply_owner_feedback(
            "pubg",
            clip_id=f"c{i}",
            is_good=False,
            reason="loot_run",
            vod_id="v1",
        )
    after = thresholds_for("pubg")["gun_density_min"]
    assert after > before
