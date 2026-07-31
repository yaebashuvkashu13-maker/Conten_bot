#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_hero_roles import (  # noqa: E402
    hero_from_text,
    highlight_search_heroes,
    is_excluded_role,
    title_is_tank_support_only,
)
from mlbb_kill_banner import (  # noqa: E402
    classify_banner_text,
    is_coordination_banner_text,
    is_enemy_kill_text,
)


def test_reject_coordination_gather() -> None:
    assert is_coordination_banner_text("Gather at lord")
    assert classify_banner_text("Gather at lord") is None
    assert classify_banner_text("Gather — DOUBLE KILL") is not None


def test_reject_coordination_russian() -> None:
    assert is_coordination_banner_text("Соберитесь у лорда")
    assert classify_banner_text("В атаку!") is None


def test_reject_enemy_slain_by() -> None:
    assert is_enemy_kill_text("You have been slain by Gusion")
    assert classify_banner_text("You have been slain by Gusion") is None
    assert is_enemy_kill_text("YOU HAVE BEEN SLAIN")
    assert classify_banner_text("Enemy has slain Layla") is None


def test_tank_support_roles() -> None:
    assert is_excluded_role("angela")
    assert is_excluded_role("tigreal")
    assert not is_excluded_role("chou")
    assert title_is_tank_support_only("Angela ranked mythic gameplay")
    assert not title_is_tank_support_only("Chou savage ranked gameplay")


def test_highlight_search_excludes_support() -> None:
    heroes = highlight_search_heroes()
    assert "angela" not in heroes
    assert "tigreal" not in heroes
    assert "chou" in heroes


def test_hero_from_title() -> None:
    assert hero_from_text("Global Chou ranked savage gameplay") == "chou"
