#!/usr/bin/env python3
"""Media integration suite for PUBG quality gates.

Real fixtures are optional on CI. When present under tests/fixtures/pubg/,
each case must accept/reject according to the label. Without fixtures the
suite documents the contract and skips.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURE_DIR = Path(
    os.environ.get(
        "PUBG_MEDIA_FIXTURES_DIR",
        Path(__file__).resolve().parent / "fixtures" / "pubg",
    )
)

CASES = {
    "kill_confirmed.mp4": {"expect_accept": True, "tag": "kill"},
    "shooting_no_kill.mp4": {"expect_accept": False, "tag": "no_kill"},
    "loot_walk.mp4": {"expect_accept": False, "tag": "loot"},
    "author_death.mp4": {"expect_accept": False, "tag": "death"},
    "movable_blue_kill.mp4": {"expect_accept": True, "tag": "movable_kill"},
    "blue_hud_false_positive.mp4": {"expect_accept": False, "tag": "blue_fp"},
    "odd_viewport.mp4": {"expect_accept": None, "tag": "viewport"},  # detect only
}


def _fixture(name: str) -> Path:
    return FIXTURE_DIR / name


@pytest.mark.parametrize("name,spec", sorted(CASES.items()))
def test_pubg_media_fixture_contract(name: str, spec: dict) -> None:
    path = _fixture(name)
    if not path.is_file():
        pytest.skip(f"missing media fixture {path}")
    # Import late so unit CI without torch/ffmpeg still collects.
    from pubg_quality_score import score_pubg_window

    ok, reason, report = score_pubg_window(path, 0.0, 12.0)
    expect = spec["expect_accept"]
    if expect is None:
        assert isinstance(report, dict) and (reason or report)
        return
    if expect:
        assert ok, f"{name} should accept, got {reason} report={report}"
    else:
        assert not ok, f"{name} should reject, got accept reason={reason}"


def test_fixture_readme_present() -> None:
    readme = FIXTURE_DIR / "README.md"
    assert readme.is_file()
    text = readme.read_text(encoding="utf-8")
    assert "kill_confirmed.mp4" in text
    assert "blue_hud_false_positive.mp4" in text
