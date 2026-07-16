"""Tests for MLBB throughput relaxation after consecutive zero-yield VODs."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mlbb_vod_segment_feed as feed  # noqa: E402


def test_relax_overrides_after_threshold(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_RELAX_AFTER_ZERO_VODS", "3")
    monkeypatch.setenv("MLBB_VOD_QUALITY_MODE_RELAX", "0")
    monkeypatch.setenv("MLBB_VOD_MIN_CLIP_SCORE_RELAX", "0.02")
    monkeypatch.setenv("MLBB_BANNER_MIN_HOOK_RELAX", "0.03")
    monkeypatch.setenv("MLBB_THROUGHPUT_SILENCE_SEC", "999999")
    monkeypatch.setattr(feed, "_last_send_age_sec", lambda: 0.0)

    assert feed._mlbb_relax_overrides(2, adaptive_streak=0) == {}
    ov = feed._mlbb_relax_overrides(3, adaptive_streak=0)
    assert ov["MLBB_VOD_QUALITY_MODE"] == "0"
    assert ov["MLBB_VOD_MIN_CLIP_SCORE"] == "0.02"
    assert ov["MLBB_BANNER_MIN_HOOK"] == "0.03"
    assert ov["MLBB_FEEDBACK_GATE"] == "0"


def test_relax_overrides_on_silence(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_RELAX_AFTER_ZERO_VODS", "9")
    monkeypatch.setenv("MLBB_THROUGHPUT_SILENCE_SEC", "1800")
    monkeypatch.setattr(feed, "_last_send_age_sec", lambda: 5000.0)
    ov = feed._mlbb_relax_overrides(0, adaptive_streak=0)
    assert ov["MLBB_VOD_QUALITY_MODE"] == "0"
    assert ov["MLBB_VOD_DISABLE_SOFTEN"] == "0"


def test_soften_l1_disables_quality_and_feedback() -> None:
    from mlbb_vod_adaptive_gate import overrides_for_level

    ov = overrides_for_level(1)
    assert ov["MLBB_VOD_QUALITY_MODE"] == "0"
    assert ov["MLBB_FEEDBACK_GATE"] == "0"
