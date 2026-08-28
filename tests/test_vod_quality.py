"""Tests for vod_quality flags."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_quality import pubg_quality_strict  # noqa: E402


def test_pubg_quality_strict_defaults_with_pubg_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOD_PUBG_QUALITY_STRICT", raising=False)
    monkeypatch.setenv("VOD_PUBG_ONLY", "1")
    assert pubg_quality_strict() is True


def test_pubg_quality_strict_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_PUBG_QUALITY_STRICT", "0")
    monkeypatch.setenv("VOD_PUBG_ONLY", "1")
    assert pubg_quality_strict() is False
