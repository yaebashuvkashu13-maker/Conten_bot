"""WoT / Genshin hook bypass when combat audio is strong."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from highlight_scorer import _accept_highlight_candidate, HighlightMetrics  # noqa: E402


def _metrics(**kwargs):
    defaults = {
        "start": 120.0,
        "duration": 10.0,
        "profile": "wot",
        "rule_pass": True,
        "visual_pass": True,
        "hook_score": 0.05,
        "panns_gun_max": 0.0,
        "panns_explosion": 0.0,
        "clip_score": 0.15,
        "pass_reason": "wot_impact_ok",
        "viral_score": 0.0,
        "heatmap_intensity": 0.0,
    }
    defaults.update(kwargs)
    return HighlightMetrics(**defaults)


def test_wot_hook_bypass_with_strong_panns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRAL_SEGMENT_HOOK_MIN", "0.08")
    monkeypatch.setenv("VIRAL_COMBAT_HOOK_MIN", "0.035")
    m = _metrics(panns_gun_max=0.43, hook_score=0.024)
    assert _accept_highlight_candidate(Path("yt_test.mp4"), 120.0, m, "wot") is True


def test_wot_hook_clip_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRAL_SEGMENT_HOOK_MIN", "0.08")
    monkeypatch.setenv("VIRAL_COMBAT_HOOK_MIN", "0.10")
    monkeypatch.setenv("HIGHLIGHT_EXTENDED_CLIP_HOOK_MIN", "0.12")
    m = _metrics(panns_explosion=0.31, hook_score=0.05, clip_score=0.18)
    assert _accept_highlight_candidate(Path("yt_test.mp4"), 90.0, m, "wot") is True


def test_wot_hook_still_blocks_weak_combat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRAL_SEGMENT_HOOK_MIN", "0.08")
    m = _metrics(panns_gun_max=0.05, panns_explosion=0.04, hook_score=0.02, clip_score=0.04)
    assert _accept_highlight_candidate(Path("yt_test.mp4"), 60.0, m, "wot") is False
