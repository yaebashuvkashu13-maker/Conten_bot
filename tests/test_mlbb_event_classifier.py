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
    cluster_own_decisions,
    confident_non_own_event,
    extract_visual_features,
    predict_visual_event,
    temporal_consensus,
)


class _FakeModel:
    def __init__(self, classes, probabilities):
        self.classes_ = np.asarray(classes)
        self._probabilities = np.asarray(probabilities, dtype=float)

    def predict_proba(self, _features):
        return self._probabilities.reshape(1, -1)


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


def test_explicit_multi_kill_beats_overlaid_command_but_not_enemy() -> None:
    own = classify_event_text("Retreat ... TRIPLE KILL")
    assert (own.kind, own.tier) == (OWN_STREAK, 3)
    assert classify_event_text("Enemy Triple Kill Retreat").kind == ENEMY_STREAK


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


def test_artifact_own_threshold_prioritizes_precision() -> None:
    frame = np.zeros((48, 160, 3), dtype=np.uint8)
    artifact = {
        "event_model": _FakeModel(
            [COMMAND, OTHER, OWN_STREAK],
            [0.2, 0.1, 0.7],
        ),
        "tier_model": _FakeModel(["2", "3", "5"], [0.05, 0.9, 0.05]),
        "own_threshold": 0.8,
    }
    assert predict_visual_event(frame, artifact=artifact).kind == COMMAND
    artifact["event_model"] = _FakeModel(
        [COMMAND, OTHER, OWN_STREAK],
        [0.05, 0.04, 0.91],
    )
    own = predict_visual_event(frame, artifact=artifact)
    assert (own.kind, own.tier) == (OWN_STREAK, 3)


def test_fast_non_own_veto_requires_very_high_confidence(monkeypatch) -> None:
    frame = np.zeros((48, 160, 3), dtype=np.uint8)
    monkeypatch.setenv("MLBB_EVENT_FAST_BLOCK_MIN_CONF", "0.95")
    with patch(
        "mlbb_event_classifier.predict_visual_event",
        return_value=EventDecision(OTHER, 0.94, source="event_model"),
    ):
        assert confident_non_own_event(frame) is None
    blocked = EventDecision(OBJECTIVE, 0.99, source="event_model")
    with patch("mlbb_event_classifier.predict_visual_event", return_value=blocked):
        assert confident_non_own_event(frame) == blocked


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


def test_full_vod_cluster_rejects_single_frame_noise() -> None:
    hit = EventDecision(
        OWN_STREAK,
        0.95,
        tier=3,
        label="triple",
        source="event_model",
        tier_confidence=0.9,
    )
    assert cluster_own_decisions([(10.0, hit)]) == []
    events = cluster_own_decisions(
        [(10.0, hit), (10.5, hit), (20.0, hit), (22.0, hit)]
    )
    assert len(events) == 1
    assert events[0][1].tier == 3
