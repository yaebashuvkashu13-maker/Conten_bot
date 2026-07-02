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
