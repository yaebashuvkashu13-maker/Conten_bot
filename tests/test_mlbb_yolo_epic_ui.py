"""Tests for YOLO epic UI label weights (no model load)."""

from __future__ import annotations

from mlbb_yolo_epic_ui import YoloEpicResult, _label_weight


def test_high_tier_savage_scores_high() -> None:
    w = _label_weight("Savage", 0.8)
    assert w >= 0.9


def test_mid_tier_slain_lower_than_savage() -> None:
    assert _label_weight("Savage", 0.7) > _label_weight("Slain", 0.7)


def test_yolo_result_defaults() -> None:
    r = YoloEpicResult()
    assert not r.detected
    assert r.reason == "yolo_unavailable"
