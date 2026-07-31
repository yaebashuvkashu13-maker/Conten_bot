#!/usr/bin/env python3
"""3lO0AHqEfxs_38: weak ref-triple on jungle farm must not ship."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_neg_not_kill_rejects_without_streak(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_BANNER_OWN_KILL_REQUIRED", "1")
    monkeypatch.setenv("MLBB_BANNER_NEG_NOT_KILL_MIN", "0.48")
    from mlbb_banner_hero_match import validate_own_kill_frame

    with patch(
        "mlbb_banner_ref_match.match_negative_banner_reference",
        return_value=(0.49, "not_kill", "/x.png"),
    ):
        ok, reason = validate_own_kill_frame(object(), ocr_text="random ui junk")
        assert ok is False
        assert "not_kill" in reason


def test_neg_not_kill_allows_real_streak_ocr(monkeypatch) -> None:
    import numpy as np

    monkeypatch.setenv("MLBB_BANNER_OWN_KILL_REQUIRED", "1")
    monkeypatch.setenv("MLBB_BANNER_NEG_NOT_KILL_MIN", "0.48")
    from mlbb_banner_hero_match import validate_own_kill_frame

    frame = np.zeros((270, 480, 3), dtype=np.uint8)
    with (
        patch(
            "mlbb_banner_ref_match.match_negative_banner_reference",
            return_value=(0.49, "not_kill", "/x.png"),
        ),
        patch("mlbb_banner_hero_match.extract_killer_portrait_patch", return_value=None),
        patch("mlbb_banner_hero_match.extract_hud_hero_portrait_patch", return_value=None),
    ):
        # Streak OCR clears the not_kill veto; subsequent icon checks may still fail.
        _ok, reason = validate_own_kill_frame(frame, ocr_text="Enemy has been slain TRIPLE KILL")
        assert "not_kill" not in reason


def test_discover_pos_min_sim_default_raised(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_BANNER_DISCOVER_ACTIVE", "1")
    monkeypatch.delenv("MLBB_BANNER_DISCOVER_POS_LIVE_MIN_SIM", raising=False)
    monkeypatch.setenv("MLBB_BANNER_POS_LIVE_MIN_SIM", "0.62")
    # Default discover floor in code is now 0.55 (was 0.38).
    assert float(os.environ.get("MLBB_BANNER_DISCOVER_POS_LIVE_MIN_SIM", "0.55")) == 0.55


def test_weak_pos_loses_to_close_neg(monkeypatch) -> None:
    """pos=0.511 vs not_kill=0.487 must drop (3lO0 case)."""
    import numpy as np

    monkeypatch.setenv("MLBB_BANNER_DISCOVER_ACTIVE", "1")
    monkeypatch.setenv("MLBB_BANNER_DISCOVER_POS_LIVE_MIN_SIM", "0.55")
    monkeypatch.setenv("MLBB_BANNER_NEG_POS_MARGIN", "0.06")
    monkeypatch.setenv("MLBB_BANNER_REF_COLOR_MUL", "0.1")
    monkeypatch.setenv("MLBB_BANNER_DISCOVER_REF_COLOR_MUL", "0.1")
    monkeypatch.setenv("MLBB_KILL_BANNER_COLOR_MIN", "0.01")

    import mlbb_banner_ref_match as rm

    frame = np.zeros((270, 480, 3), dtype=np.uint8)
    with (
        patch.object(rm, "match_negative_banner_reference", return_value=(0.487, "not_kill", "n.png")),
        patch.object(
            rm,
            "match_positive_owner_reference",
            return_value=(0.511, "double_triple", "Y3In5vMdlak_420.png"),
        ),
        patch.object(rm, "match_banner_reference", return_value=None),
        patch("mlbb_kill_banner._announce_color_score", return_value=0.5),
        patch("mlbb_kill_banner._color_min_score", return_value=0.04),
        patch("mlbb_kill_banner._ref_classify_min_tier", return_value=1),
        patch("mlbb_kill_banner._discover_active", return_value=True),
    ):
        hit = rm.classify_banner_reference(58.0, frame, vod=None)
        assert hit is None
