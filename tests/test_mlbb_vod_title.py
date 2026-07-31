#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_vod_title import (  # noqa: E402
    title_kill_count,
    title_min_banner_tier,
    title_promises_kill_streak,
    title_scan_start_sec,
    vod_title_blob,
)


def test_savage_title_requires_tier_5() -> None:
    assert title_min_banner_tier("insane savage gameplay 26 kills") == 5
    assert title_promises_kill_streak("legendary maniac montage")


def test_maniac_title_requires_tier_4() -> None:
    assert title_min_banner_tier("0.5s wanwan maniac best build") == 4


def test_double_title_requires_tier_2() -> None:
    assert title_min_banner_tier("double kill highlight") == 2


def test_neutral_title_no_tier() -> None:
    assert title_min_banner_tier("mobile legends ranked gameplay") == 0


def test_enemy_savage_title_not_forced() -> None:
    assert title_min_banner_tier("my gusion after enemy paquito got 2x savages") == 0


def test_title_scan_start_early_for_savage() -> None:
    assert title_scan_start_sec("savage montage", 180.0) == 3.0
    assert title_scan_start_sec("ranked gameplay", 600.0) is None
    assert title_scan_start_sec("hyper alice 21 kills unstoppable", 638.0) == 3.0


def test_title_promises_kill_streak_on_high_kills() -> None:
    assert title_promises_kill_streak("21 kills mvp gameplay")


def test_title_kill_count_hyphenated() -> None:
    # Cv7Ul8t6j6s-style titles: "Hyper 11-Kill Super Carry"
    assert title_kill_count("Paquito Hyper 11-Kill Super Carry vs Harley") == 11
    assert title_kill_count("25 Kills!! Legendary Paquito") == 25
    assert title_promises_kill_streak("Paquito Hyper 11-Kill Super Carry")


def test_vod_title_blob_from_env(monkeypatch, tmp_path: Path) -> None:
    vod = tmp_path / "yt_ABC123.mp4"
    vod.touch()
    monkeypatch.setenv("MLBB_VOD_SCAN_TITLE", "INSANE SAVAGE 30 KILLS")
    blob = vod_title_blob(vod)
    assert "savage" in blob
    assert "abc123" in blob


def test_title_gate_disabled() -> None:
    old = os.environ.get("MLBB_TITLE_SAVAGE_MIN_TIER")
    os.environ["MLBB_TITLE_SAVAGE_MIN_TIER"] = "0"
    try:
        assert title_min_banner_tier("savage") == 0
    finally:
        if old is None:
            os.environ.pop("MLBB_TITLE_SAVAGE_MIN_TIER", None)
        else:
            os.environ["MLBB_TITLE_SAVAGE_MIN_TIER"] = old
