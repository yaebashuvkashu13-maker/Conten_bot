"""Tests for cheap support/roam rejection before MLBB VOD download."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_hero_roles import (  # noqa: E402
    heroes_from_title,
    passes_vod_hero_gate,
    support_heroes_from_title,
    vod_hero_rank_adjustment,
)


def test_rejects_support_without_multikill_signal() -> None:
    ok, reason = passes_vod_hero_gate(
        "Angela Roam Mythic Ranked Full Match Gameplay MLBB"
    )
    assert ok is False
    assert reason == "support_without_multikill:angela"


def test_rejects_generic_roam_query_result() -> None:
    ok, reason = passes_vod_hero_gate("MLBB Roam Mythic Ranked Full Game")
    assert ok is False
    assert reason == "support_without_multikill:roam"


def test_allows_support_when_title_proves_multikill() -> None:
    ok, reason = passes_vod_hero_gate(
        "Angela MVP 18 Kills Savage Mythic Ranked MLBB"
    )
    assert ok is True
    assert reason == "support_with_combat_signal"


def test_carry_heroes_are_not_blocked() -> None:
    title = "Hayabusa 20 Kills Savage Mythic Full Match MLBB"
    assert heroes_from_title(title) == ["hayabusa"]
    assert support_heroes_from_title(title) == []
    assert passes_vod_hero_gate(title)[0] is True
    assert vod_hero_rank_adjustment(title) == 0.0


def test_support_without_action_gets_large_rank_penalty() -> None:
    assert vod_hero_rank_adjustment("Tigreal Tank Mythic Ranked MLBB") <= -10
