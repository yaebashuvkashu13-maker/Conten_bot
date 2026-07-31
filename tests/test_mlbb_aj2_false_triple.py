#!/usr/bin/env python3
"""AJ2o2jHhNfE_414: false ref-triple on Turtle + short post-cut."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_coordination_matches_take_turtle() -> None:
    from mlbb_kill_banner import is_coordination_banner_text

    assert is_coordination_banner_text("Take Turtle Gather")
    assert is_coordination_banner_text("please gather near me")
    assert not is_coordination_banner_text("TRIPLE KILL")


def test_finalize_rejects_ref_when_live_is_turtle(monkeypatch) -> None:
    from unittest.mock import patch

    import mlbb_kill_banner as kb

    monkeypatch.setenv("MLBB_BANNER_OWN_KILL_REQUIRED", "1")
    monkeypatch.setenv("MLBB_BANNER_LIVE_OVERLAY_OCR", "1")
    hit = kb.KillBannerHit(sec=425.0, tier=3, label="triple", text="TRIPLE KILL", source="ref")
    frame = object()

    with (
        patch.object(kb, "_live_overlay_text", return_value="Take Turtle Gather"),
        patch("mlbb_banner_hero_match.validate_own_kill_frame") as mock_own,
    ):
        out = kb._finalize_banner_hit(frame, hit, vod=None)
        assert out is None
        mock_own.assert_not_called()


def test_bounds_post_default_short() -> None:
    os.environ["MLBB_BANNER_POST_SEC"] = "1.5"
    os.environ["MLBB_DOUBLE_BANNER_POST_SEC"] = "1.5"
    os.environ["MLBB_KILL_BANNER_LEAD_SEC"] = "8"
    os.environ["MLBB_BANNER_IDEAL_MIN"] = "1"
    os.environ["MLBB_BANNER_HARD_POST_CUT"] = "1"
    os.environ["MLBB_FIGHT_MIN_SEC"] = "7"
    from mlbb_kill_banner import bounds_from_banner

    start, end, dur = bounds_from_banner(433.0, 834.0, banner_tier=2)
    assert end == 434.5
    assert end - 433.0 <= 1.6


def test_bounds_double_post_covers_combo() -> None:
    os.environ["MLBB_BANNER_POST_SEC"] = "1.5"
    os.environ.pop("MLBB_DOUBLE_BANNER_POST_SEC", None)
    os.environ["MLBB_KILL_BANNER_LEAD_SEC"] = "8"
    os.environ["MLBB_BANNER_IDEAL_MIN"] = "0"
    os.environ["MLBB_BANNER_HARD_POST_CUT"] = "1"
    from mlbb_kill_banner import bounds_from_banner

    _s, end, _d = bounds_from_banner(433.0, 834.0, banner_tier=2)
    assert end >= 437.0
