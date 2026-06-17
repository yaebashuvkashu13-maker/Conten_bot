"""MLBB classifier feature schema — train must match inference."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from highlight_scorer import HighlightMetrics  # noqa: E402
from mlbb_classifier_features import (  # noqa: E402
    MLBB_CLASSIFIER_FEATURE_NAMES,
    attach_classifier_metadata,
    classifier_schema_compatible,
    mlbb_classifier_feature_vector,
    mlbb_combined_score,
)


def test_train_inference_feature_vector_match() -> None:
    m = HighlightMetrics(
        start=10.0,
        duration=15.0,
        profile="mobile_legends",
        clip_score=0.42,
        minimap_delta=0.018,
        skill_delta=0.012,
        center_motion=0.035,
        hook_score=0.55,
        visual_dynamics=0.21,
    )
    vec = mlbb_classifier_feature_vector(m)
    assert vec == [0.42, 0.018, 0.012, 0.035, 0.55, 0.21]
    assert len(vec) == len(MLBB_CLASSIFIER_FEATURE_NAMES)


def test_mlbb_combined_score_not_gun_based() -> None:
    m = HighlightMetrics(
        start=0.0,
        duration=12.0,
        profile="mobile_legends",
        panns_gun_max=0.0,
        minimap_delta=0.02,
        skill_delta=0.015,
        hook_score=0.4,
        clip_score=0.3,
        center_motion=0.03,
    )
    score = mlbb_combined_score(m, classifier_prob=0.6)
    assert score > 0.15


def test_classifier_schema_rejects_wrong_n_features() -> None:
    class _FakeClf:
        n_features_in_ = 2
        mlbb_schema_version = 1

    assert not classifier_schema_compatible(_FakeClf())

    class _OkClf:
        n_features_in_ = 6
        mlbb_schema_version = 1

    assert classifier_schema_compatible(_OkClf())


def test_attach_metadata() -> None:
    class _Clf:
        pass

    clf = attach_classifier_metadata(_Clf())
    assert clf.profile == "mobile_legends"
    assert clf.feature_names == list(MLBB_CLASSIFIER_FEATURE_NAMES)
