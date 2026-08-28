"""Tests for vod_quality yield helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_quality import dense_probe_passes, montages_per_vod, pubg_quality_strict  # noqa: E402


def test_montages_per_vod_strict_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHOOTER_VOD_MONTAGES_PER_VOD", raising=False)
    monkeypatch.setenv("VOD_PUBG_QUALITY_STRICT", "1")
    assert montages_per_vod("pubg") == 3


def test_montages_per_vod_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_MONTAGES_PER_VOD", "5")
    assert montages_per_vod("pubg") == 5


def test_dense_probe_passes_strict_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHOOTER_VOD_DENSE_PROBE_PASSES", raising=False)
    monkeypatch.setenv("VOD_PUBG_QUALITY_STRICT", "1")
    assert dense_probe_passes() == 2


def test_pubg_quality_strict_defaults_with_pubg_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOD_PUBG_QUALITY_STRICT", raising=False)
    monkeypatch.setenv("VOD_PUBG_ONLY", "1")
    assert pubg_quality_strict() is True
