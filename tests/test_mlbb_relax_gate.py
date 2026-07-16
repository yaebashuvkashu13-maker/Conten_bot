"""Tests for MLBB throughput relaxation after consecutive zero-yield VODs."""

from __future__ import annotations

import os
import sys

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mlbb_vod_segment_feed as feed  # noqa: E402


def test_relax_overrides_after_threshold(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_RELAX_AFTER_ZERO_VODS", "3")
    monkeypatch.setenv("MLBB_VOD_QUALITY_MODE_RELAX", "0")
    monkeypatch.setenv("MLBB_VOD_MIN_CLIP_SCORE_RELAX", "0.02")
    monkeypatch.setenv("MLBB_BANNER_MIN_HOOK_RELAX", "0.04")

    assert feed._mlbb_relax_overrides(2) == {}
    ov = feed._mlbb_relax_overrides(3)
    assert ov["MLBB_VOD_QUALITY_MODE"] == "0"
    assert ov["MLBB_VOD_MIN_CLIP_SCORE"] == "0.02"
    assert ov["MLBB_BANNER_MIN_HOOK"] == "0.04"

