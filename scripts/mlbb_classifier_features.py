#!/usr/bin/env python3
"""Shared MLBB classifier feature schema — train and inference must match."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

MLBB_CLASSIFIER_SCHEMA_VERSION = 1
MLBB_CLASSIFIER_PROFILE = "mobile_legends"
MLBB_CLASSIFIER_FEATURE_NAMES: tuple[str, ...] = (
    "clip_score",
    "minimap_delta",
    "skill_delta",
    "center_motion",
    "hook_score",
    "visual_dynamics",
)


class _MetricsLike(Protocol):
    clip_score: float
    minimap_delta: float
    skill_delta: float
    center_motion: float
    hook_score: float
    visual_dynamics: float


def _f(row: Mapping[str, Any] | _MetricsLike, key: str, default: float = 0.0) -> float:
    if isinstance(row, Mapping):
        val = row.get(key, default)
    else:
        val = getattr(row, key, default)
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def mlbb_classifier_feature_vector(row: Mapping[str, Any] | _MetricsLike) -> list[float]:
    """Single row feature vector for LogisticRegression (MLBB)."""
    return [
        _f(row, "clip_score"),
        _f(row, "minimap_delta"),
        _f(row, "skill_delta"),
        _f(row, "center_motion"),
        _f(row, "hook_score"),
        _f(row, "visual_dynamics"),
    ]


def mlbb_combined_score(row: Mapping[str, Any] | _MetricsLike, *, classifier_prob: float = 0.0) -> float:
    """MOBA teamfight ranking — not gun-based."""
    mini = _f(row, "minimap_delta")
    skill = _f(row, "skill_delta")
    hook = _f(row, "hook_score")
    clip = max(0.0, _f(row, "clip_score"))
    motion = _f(row, "center_motion")
    return float(
        mini * 2.2
        + skill * 2.0
        + hook * 0.35
        + clip * 0.30
        + motion * 0.40
        + max(0.0, classifier_prob) * 0.15
    )


def attach_classifier_metadata(clf: Any) -> Any:
    clf.mlbb_schema_version = MLBB_CLASSIFIER_SCHEMA_VERSION
    clf.feature_names = list(MLBB_CLASSIFIER_FEATURE_NAMES)
    clf.profile = MLBB_CLASSIFIER_PROFILE
    return clf


def classifier_schema_compatible(clf: Any) -> bool:
    if clf is None:
        return False
    expected = len(MLBB_CLASSIFIER_FEATURE_NAMES)
    n_in = getattr(clf, "n_features_in_", None)
    if n_in is not None and int(n_in) != expected:
        return False
    ver = getattr(clf, "mlbb_schema_version", None)
    if ver is not None and int(ver) != MLBB_CLASSIFIER_SCHEMA_VERSION:
        return False
    return True
