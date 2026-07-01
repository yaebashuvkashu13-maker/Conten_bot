"""Parallel highlight scoring worker count."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from highlight_scorer import _parallel_workers  # noqa: E402


def test_parallel_workers_env_override(monkeypatch) -> None:
    monkeypatch.setenv("HIGHLIGHT_PARALLEL_WORKERS", "4")
    assert _parallel_workers() == 4


def test_parallel_workers_default_uses_most_cores(monkeypatch) -> None:
    monkeypatch.delenv("HIGHLIGHT_PARALLEL_WORKERS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert _parallel_workers() == 6


def test_window_score_timeout_defaults(monkeypatch) -> None:
    from highlight_scorer import _parallel_batch_timeout_sec, _window_score_timeout_sec

    monkeypatch.delenv("HIGHLIGHT_WINDOW_SCORE_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("HIGHLIGHT_PARALLEL_BATCH_TIMEOUT_SEC", raising=False)
    assert _window_score_timeout_sec() == 480.0
    assert _parallel_batch_timeout_sec(5, 6) == 510.0


def test_parallel_batch_timeout_respects_cap(monkeypatch) -> None:
    from highlight_scorer import _parallel_batch_timeout_sec

    monkeypatch.setenv("HIGHLIGHT_WINDOW_SCORE_TIMEOUT_SEC", "600")
    monkeypatch.setenv("HIGHLIGHT_PARALLEL_BATCH_TIMEOUT_SEC", "300")
    assert _parallel_batch_timeout_sec(5, 6) == 300.0
