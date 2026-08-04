from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mlbb_event_classifier import (  # noqa: E402
    ALLY_STREAK,
    COMMAND,
    ENEMY_STREAK,
    OBJECTIVE,
    OTHER,
    OWN_STREAK,
    EventDecision,
    classify_event,
    classify_event_text,
    extract_visual_features,
    temporal_consensus,
)


def test_text_rules_accept_only_own_streaks() -> None:
    own = classify_event_text("TRIPLE KILL")
    assert (own.kind, own.tier) == (OWN_STREAK, 3)
    assert classify_event_text("Enemy Triple Kill").kind == ENEMY_STREAK
    assert classify_event_text("Ally Maniac").kind == ALLY_STREAK


def test_text_rules_block_objectives_and_commands() -> None:
    for text in ("Lord has been slain", "Turtle appeared", "Черепаха убита"):
        assert classify_event_text(text).kind == OBJECTIVE
    for text in ("Gather!", "Retreat", "В атаку", "Request backup"):
        assert classify_event_text(text).kind == COMMAND


def test_visual_features_have_stable_shape() -> None:
    crop = np.zeros((48, 160, 3), dtype=np.uint8)
    full = np.zeros((270, 480, 3), dtype=np.uint8)
    assert extract_visual_features(crop).shape == extract_visual_features(full).shape
    assert extract_visual_features(crop).ndim == 1


def test_high_confidence_visual_negative_vetoes_noisy_ocr() -> None:
    frame = np.zeros((48, 160, 3), dtype=np.uint8)
    visual = EventDecision(COMMAND, 0.95, label="command", source="event_model")
    with patch("mlbb_event_classifier.predict_visual_event", return_value=visual):
        decision = classify_event("KILL", frame)
    assert decision.kind == COMMAND
    assert decision.source == "visual_veto"


def test_visual_recovery_requires_tier_and_high_confidence() -> None:
    frame = np.zeros((48, 160, 3), dtype=np.uint8)
    good = EventDecision(
        OWN_STREAK,
        0.93,
        tier=3,
        label="triple",
        source="event_model",
        tier_confidence=0.88,
    )
    with patch("mlbb_event_classifier.predict_visual_event", return_value=good):
        assert classify_event("", frame).kind == OWN_STREAK
    weak = EventDecision(
        OWN_STREAK,
        0.60,
        tier=3,
        label="triple",
        source="event_model",
        tier_confidence=0.9,
    )
    with patch("mlbb_event_classifier.predict_visual_event", return_value=weak):
        assert classify_event("", frame).kind == OTHER


def test_temporal_consensus_requires_two_model_frames() -> None:
    hit = EventDecision(
        OWN_STREAK,
        0.92,
        tier=2,
        label="double",
        source="event_model",
        tier_confidence=0.8,
    )
    assert temporal_consensus([(10.0, hit)]) is None
    assert temporal_consensus([(10.0, hit), (10.5, hit)]) == hit


def test_explicit_ocr_hit_needs_only_one_frame() -> None:
    hit = EventDecision(
        OWN_STREAK,
        1.0,
        tier=5,
        label="savage",
        source="ocr_rules",
        tier_confidence=1.0,
    )
    assert temporal_consensus([(10.0, hit)]) == hit
