#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_kill_banner import _effective_discover_min_tier  # noqa: E402


def test_discover_title_tier_capped_at_merge_by_default(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_KILL_BANNER_DISCOVER_MERGE_TIER", "1")
    monkeypatch.delenv("MLBB_KILL_BANNER_DISCOVER_TITLE_CAP", raising=False)
    assert _effective_discover_min_tier(5) == 1
    assert _effective_discover_min_tier(2) == 1


def test_discover_title_force_can_raise(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_KILL_BANNER_DISCOVER_MERGE_TIER", "1")
    monkeypatch.setenv("MLBB_VOD_TITLE_FORCE_DISCOVER_TIER", "1")
    monkeypatch.setenv("MLBB_KILL_BANNER_DISCOVER_TITLE_CAP", "5")
    assert _effective_discover_min_tier(5) == 5
