"""Tests for grouped model evaluation and promotion helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_model_training import (  # noqa: E402
    evaluate_binary,
    grouped_holdout_indices,
    passes_quality_gate,
)


class _ProbModel:
    def __init__(self, probabilities: list[float]) -> None:
        self.probabilities = probabilities

    def predict_proba(self, _X):
        return np.asarray([[1.0 - p, p] for p in self.probabilities])


def test_grouped_holdout_never_leaks_vod_groups() -> None:
    labels = [1, 0, 1, 0, 1, 0, 1, 0]
    groups = ["a", "a", "b", "b", "c", "c", "d", "d"]
    train, test = grouped_holdout_indices(labels, groups, test_fraction=0.5)
    train_groups = {groups[i] for i in train}
    test_groups = {groups[i] for i in test}
    assert train_groups.isdisjoint(test_groups)
    assert {labels[i] for i in train} == {0, 1}
    assert {labels[i] for i in test} == {0, 1}


def test_quality_gate_requires_precision_recall_and_bad_rejection(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_TRAIN_MIN_PRECISION", "0.85")
    monkeypatch.setenv("MLBB_TRAIN_MIN_RECALL", "0.70")
    monkeypatch.setenv("MLBB_TRAIN_MAX_BAD_FALSE_PASS", "0.10")

    metrics = evaluate_binary(
        _ProbModel([0.9, 0.8, 0.1, 0.2]),
        np.zeros((4, 2)),
        np.asarray([1, 1, 0, 0]),
    )
    passed, failures = passes_quality_gate(metrics)
    assert passed is True
    assert failures == []

    failed, reasons = passes_quality_gate(
        {"precision": 0.5, "recall": 0.9, "bad_false_pass": 0.5}
    )
    assert failed is False
    assert any(reason.startswith("precision") for reason in reasons)
    assert any(reason.startswith("bad_false_pass") for reason in reasons)
