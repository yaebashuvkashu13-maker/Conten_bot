"""Strong-ref DOUBLE solo under silence — gold-blind OCR must not park every VOD."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_strong_ref_double_allowed_under_silence(monkeypatch) -> None:
    import numpy as np

    monkeypatch.setenv("MLBB_VOD_KILL_BANNER", "1")
    monkeypatch.setenv("MLBB_PRESEND_OWN_KILL_RECHECK", "1")
    monkeypatch.setenv("MLBB_BANNER_SEND_MIN_TIER", "double")
    monkeypatch.setenv("MLBB_SOLO_REQUIRE_LIVE_MULTI", "1")
    monkeypatch.setenv("MLBB_SOLO_ALLOW_STRONG_REF", "1")
    monkeypatch.setenv("MLBB_SOLO_STRONG_REF_DOUBLE", "1")
    monkeypatch.setenv("MLBB_SOLO_STRONG_REF_DOUBLE_HUD_MIN", "0.55")
    monkeypatch.setenv("MLBB_VOD_BANNER_PRESEND", "0")
    monkeypatch.setenv("MLBB_PRESEND_MONTAGE_SINGLE", "0")
    monkeypatch.setenv("MLBB_PRESEND_REJECT_LIVE_SINGLE", "0")
    monkeypatch.setenv("MLBB_OCR_DOUBLE_REQUIRE_LIVE", "0")
    monkeypatch.setenv("MLBB_BANNER_REJECT_OCR_SINGLE", "0")
    monkeypatch.setenv("MLBB_PRESEND_MIN_BANNER_SEC", "90")
    monkeypatch.setenv("MLBB_KILL_BANNER_LEAD_SEC", "8")
    monkeypatch.setenv("MLBB_BANNER_POST_SEC", "2")

    import mlbb_vod_segment_feed as feed

    frame = np.zeros((270, 480, 3), dtype=np.uint8)
    row = {
        "segment_id": "fake_200",
        "start": 200.0,
        "peak_start": 208.0,
        "banner_sec": 208.0,
        "duration": 12.0,
        "kill_banner_tier": 2,
        "kill_banner": "double",
        "kill_banner_text": "DOUBLE KILL",
        "banner_text": "DOUBLE KILL",
        "banner_source": "ref",
    }
    with (
        patch.object(feed, "_detect_render_freeze", return_value=(True, "ok", [])),
        patch("gameplay_gate._read_frame_at", return_value=frame),
        patch(
            "mlbb_kill_banner._live_overlay_text",
            return_value="07:36 HUD SOUP digits 15 24 12",
        ),
        patch(
            "mlbb_banner_hero_match.validate_own_kill_frame",
            return_value=(True, "hud_killer_ok:0.58"),
        ),
        patch("mlbb_kill_banner.classify_banner_text", return_value=None),
        patch(
            "gameplay_gate.score_segment_combat",
            return_value=(0.05, 0.05, 0.05, ""),
        ),
        patch("gameplay_gate.segment_looks_like_draft_or_queue", return_value=False),
        patch.object(feed, "_vod_crop_box", return_value=None),
        patch(
            "gameplay_gate.segment_uniform_gameplay_ok",
            return_value=(True, "ok"),
        ),
        patch(
            "visual_action_check.extract_and_check_segment",
            return_value={"visual_pass": True, "fail_reason": ""},
        ),
    ):
        ok, reason, report = feed._validate_before_send(
            Path("x.mp4"), row, Path("y.mp4")
        )
    assert ok is True, reason
    assert report.get("solo_strong_ref") is True


def test_strong_ref_double_blocked_weak_hud(monkeypatch) -> None:
    """B9L4 guard: hud=0.366 must stay blocked even with DOUBLE silence flag."""
    import numpy as np

    monkeypatch.setenv("MLBB_VOD_KILL_BANNER", "1")
    monkeypatch.setenv("MLBB_PRESEND_OWN_KILL_RECHECK", "1")
    monkeypatch.setenv("MLBB_BANNER_SEND_MIN_TIER", "double")
    monkeypatch.setenv("MLBB_SOLO_REQUIRE_LIVE_MULTI", "1")
    monkeypatch.setenv("MLBB_SOLO_ALLOW_STRONG_REF", "1")
    monkeypatch.setenv("MLBB_SOLO_STRONG_REF_DOUBLE", "1")
    monkeypatch.setenv("MLBB_SOLO_STRONG_REF_DOUBLE_HUD_MIN", "0.55")
    monkeypatch.setenv("MLBB_VOD_BANNER_PRESEND", "0")
    monkeypatch.setenv("MLBB_PRESEND_MONTAGE_SINGLE", "0")
    monkeypatch.setenv("MLBB_OCR_DOUBLE_REQUIRE_LIVE", "0")
    monkeypatch.setenv("MLBB_BANNER_REJECT_OCR_SINGLE", "0")
    monkeypatch.setenv("MLBB_PRESEND_MIN_BANNER_SEC", "90")
    monkeypatch.setenv("MLBB_PRESEND_REJECT_LIVE_SINGLE", "0")

    import mlbb_vod_segment_feed as feed

    frame = np.zeros((270, 480, 3), dtype=np.uint8)
    row = {
        "segment_id": "B9L4_fake",
        "start": 200.0,
        "peak_start": 208.0,
        "banner_sec": 208.0,
        "duration": 12.0,
        "kill_banner_tier": 2,
        "kill_banner": "double",
        "kill_banner_text": "DOUBLE KILL",
        "banner_source": "ref",
    }
    with (
        patch.object(feed, "_detect_render_freeze", return_value=(True, "ok", [])),
        patch("gameplay_gate._read_frame_at", return_value=frame),
        patch(
            "mlbb_kill_banner._live_overlay_text",
            return_value="07:36 HUD SOUP digits 15 24 12",
        ),
        patch(
            "mlbb_banner_hero_match.validate_own_kill_frame",
            return_value=(True, "hud_killer_ok:0.366"),
        ),
        patch("mlbb_kill_banner.classify_banner_text", return_value=None),
        patch(
            "gameplay_gate.score_segment_combat",
            return_value=(0.05, 0.05, 0.05, ""),
        ),
        patch("gameplay_gate.segment_looks_like_draft_or_queue", return_value=False),
        patch.object(feed, "_vod_crop_box", return_value=None),
        patch(
            "gameplay_gate.segment_uniform_gameplay_ok",
            return_value=(True, "ok"),
        ),
        patch(
            "visual_action_check.extract_and_check_segment",
            return_value={"visual_pass": True, "fail_reason": ""},
        ),
    ):
        ok, reason, _report = feed._validate_before_send(
            Path("x.mp4"), row, Path("y.mp4")
        )
    assert ok is False
    assert "solo_needs_live_multi" in reason


def test_hunt_skips_hold_barren_dirs() -> None:
    from pathlib import Path

    skip_dirs = {"hold_barren", "hold_quota", "park_dead", "exhausted", "hold"}
    barren = Path("/root/data/mlbb/youtube_nightly/hold_barren/yt_8LNjsK7IzCY.mp4")
    inbox = Path("/root/data/mlbb/youtube_nightly/inbox/yt_good.mp4")
    assert any(part in skip_dirs for part in barren.parts)
    assert not any(part in skip_dirs for part in inbox.parts)
