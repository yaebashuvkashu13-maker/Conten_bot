#!/usr/bin/env python3
"""2Ww5h0ffYtY_270: OCR 'double' on Lord Spawn / jungle farm must not ship."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_lord_spawned_is_coordination() -> None:
    from mlbb_kill_banner import is_coordination_banner_text

    assert is_coordination_banner_text("2909:5115 Lord Spawned")
    assert is_coordination_banner_text("El Lord ha Aparecido")
    assert not is_coordination_banner_text("DOUBLE KILL")


def test_presend_rejects_ocr_double_without_live_streak(monkeypatch) -> None:
    import numpy as np

    monkeypatch.setenv("MLBB_VOD_KILL_BANNER", "1")
    monkeypatch.setenv("MLBB_PRESEND_OWN_KILL_RECHECK", "1")
    monkeypatch.setenv("MLBB_OCR_DOUBLE_REQUIRE_LIVE", "1")
    monkeypatch.setenv("MLBB_BANNER_REJECT_OCR_SINGLE", "0")
    monkeypatch.setenv("MLBB_KILL_BANNER_LEAD_SEC", "12")
    monkeypatch.setenv("MLBB_BANNER_POST_SEC", "1.5")

    import mlbb_vod_segment_feed as feed

    frame = np.zeros((270, 480, 3), dtype=np.uint8)
    row = {
        "segment_id": "2Ww5h0ffYtY_270",
        "start": 276.0,
        "peak_start": 284.0,
        "banner_sec": 284.0,
        "duration": 10.0,
        "kill_banner_tier": 2,
        "kill_banner": "double",
        "banner_source": "ocr",
    }
    with (
        patch.object(feed, "_detect_render_freeze", return_value=(True, "ok", [])),
        patch("gameplay_gate._read_frame_at", return_value=frame),
        patch(
            "mlbb_kill_banner._live_overlay_text",
            return_value="2909:5315 jungle farm names only",
        ),
        patch(
            "mlbb_banner_hero_match.validate_own_kill_frame",
            return_value=(True, "hud_killer_ok:0.50"),
        ) as mock_own,
    ):
        ok, reason, _report = feed._validate_before_send(Path("x.mp4"), row, Path("y.mp4"))
        assert ok is False
        assert "ocr_multi_no_live_streak" in reason
        mock_own.assert_called()


def test_presend_allows_ref_multi_when_hud_ok_even_if_live_ocr_blind(monkeypatch) -> None:
    """UGu: empty/ref source + own HUD must not die on clock OCR garbage."""
    import numpy as np

    monkeypatch.setenv("MLBB_VOD_KILL_BANNER", "1")
    monkeypatch.setenv("MLBB_PRESEND_OWN_KILL_RECHECK", "1")
    monkeypatch.setenv("MLBB_OCR_DOUBLE_REQUIRE_LIVE", "1")
    monkeypatch.setenv("MLBB_BANNER_REJECT_OCR_SINGLE", "0")
    monkeypatch.setenv("MLBB_KILL_BANNER_LEAD_SEC", "12")
    monkeypatch.setenv("MLBB_BANNER_POST_SEC", "1.5")

    import mlbb_vod_segment_feed as feed

    frame = np.zeros((270, 480, 3), dtype=np.uint8)
    for src in ("ref", ""):
        row = {
            "segment_id": "UGu-LYZ-GLY_270",
            "start": 302.0,
            "peak_start": 310.0,
            "banner_sec": 310.0,
            "duration": 10.0,
            "kill_banner_tier": 5,
            "kill_banner": "savage",
            "banner_source": src,
        }
        with (
            patch.object(feed, "_detect_render_freeze", return_value=(True, "ok", [])),
            patch("gameplay_gate._read_frame_at", return_value=frame),
            patch(
                "mlbb_kill_banner._live_overlay_text",
                return_value="36ms 28 15:49 26 13 19.22 X1415",
            ),
            patch(
                "mlbb_banner_hero_match.validate_own_kill_frame",
                return_value=(True, "hud_killer_ok:0.42"),
            ),
            # If we reach this helper, ocr_multi_no_live_streak did not fire.
            # The outer try/except turns this into own_kill_recheck_error.
            patch(
                "mlbb_kill_banner.ocr_weak_needs_hud",
                side_effect=RuntimeError("past_ocr_multi_gate"),
            ),
        ):
            ok, reason, _report = feed._validate_before_send(Path("x.mp4"), row, Path("y.mp4"))
            assert ok is False
            assert "ocr_multi_no_live_streak" not in reason, (src, reason)
            assert "past_ocr_multi_gate" in reason, (src, reason)


def test_presend_rejects_near_lord_spawn(monkeypatch) -> None:
    import numpy as np

    monkeypatch.setenv("MLBB_VOD_KILL_BANNER", "1")
    monkeypatch.setenv("MLBB_PRESEND_OWN_KILL_RECHECK", "1")
    monkeypatch.setenv("MLBB_KILL_BANNER_LEAD_SEC", "12")
    monkeypatch.setenv("MLBB_BANNER_POST_SEC", "1.5")

    import mlbb_vod_segment_feed as feed

    frame = np.zeros((270, 480, 3), dtype=np.uint8)
    row = {
        "segment_id": "2Ww5h0ffYtY_270",
        "start": 276.0,
        "peak_start": 284.0,
        "banner_sec": 284.0,
        "duration": 10.0,
        "kill_banner_tier": 2,
        "kill_banner": "double",
        "banner_source": "ocr",
    }

    def _live(_fr):
        # First call (banner_sec) is junk; neighbor offset finds Lord Spawned.
        return getattr(_live, "n", "")

    calls = {"n": 0}

    def live_side_effect(_fr, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "2909:5315 farm junk"
        return "Lord Spawned"

    with (
        patch.object(feed, "_detect_render_freeze", return_value=(True, "ok", [])),
        patch("gameplay_gate._read_frame_at", return_value=frame),
        patch("mlbb_kill_banner._live_overlay_text", side_effect=live_side_effect),
        patch("mlbb_banner_hero_match.validate_own_kill_frame") as mock_own,
    ):
        ok, reason, _report = feed._validate_before_send(Path("x.mp4"), row, Path("y.mp4"))
        assert ok is False
        assert "live_coordination" in reason
        mock_own.assert_not_called()
