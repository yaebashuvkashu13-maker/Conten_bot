"""Banner fast-ship rows must not crash caption/index on missing score keys."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_format_send_report_without_score_keys() -> None:
    from mlbb_vod_segment_feed import _format_send_report

    row = {
        "start": 100.0,
        "peak_start": 120.0,
        "pass_reason": "banner_fast_ship",
    }
    text = _format_send_report(row, {"pass_reason": "presend_ok", "cut_motion": 0.05})
    assert "score=0.0000" in text
    assert "hook=0.000" in text
    assert "cut@100s" in text
    assert "peak@120s" in text


def test_clip_from_banner_seed_skips_full_normalize() -> None:
    """Fast-ship must not call resolve_fight_bounds / analyze_video."""
    from mlbb_vod_segment_feed import _clip_from_banner_seed

    vod = Path("/tmp/fake_vod.mp4")

    with (
        patch.dict("os.environ", {"MLBB_BANNER_FAST_SHIP_REQUICK": "0"}, clear=False),
        patch("mlbb_vod_segment_feed._ffprobe_duration", return_value=900.0),
        patch("mlbb_kill_banner.find_banner_near_peak") as requick,
        patch("mlbb_vod_segment_feed._normalize_clip") as normalize,
        patch("mlbb_fight_segment._analysis_for") as analysis,
    ):
        clip = _clip_from_banner_seed(vod, 253.5)

    assert clip.get("banner_reject") is None
    assert clip["banner_source"] == "ref"
    assert clip["kill_banner_tier"] == 2
    assert clip["kill_banner"] == "double"
    assert clip["banner_sec"] == 253.5
    assert clip["peak_start"] == 253.5
    assert float(clip["input_duration"]) >= 7.0
    assert 253.5 - float(clip["start"]) >= 4.5
    assert float(clip.get("clip_score") or 0) == 0.0  # unscored, not fake 0.55
    assert float(clip.get("fight_end") or 0) - 253.5 <= 3.5  # ~+3s post banner
    requick.assert_not_called()
    normalize.assert_not_called()
    analysis.assert_not_called()


def test_clip_from_banner_seed_requick_keeps_seed_on_miss() -> None:
    from mlbb_vod_segment_feed import _clip_from_banner_seed

    vod = Path("/tmp/fake_vod.mp4")
    with (
        patch.dict("os.environ", {"MLBB_BANNER_FAST_SHIP_REQUICK": "1"}, clear=False),
        patch("mlbb_vod_segment_feed._ffprobe_duration", return_value=900.0),
        patch("mlbb_kill_banner.find_banner_near_peak", return_value=None),
    ):
        clip = _clip_from_banner_seed(vod, 120.8)

    assert clip.get("banner_reject") is None
    assert clip["banner_sec"] == 120.8
    assert clip["banner_source"] == "ref"
    assert clip["kill_banner_tier"] == 2


def test_banner_fast_ship_rejects_short_vod_and_early_peak() -> None:
    from mlbb_vod_segment_feed import _banner_fast_ship_seed_ok

    vod = Path("/tmp/fake_vod.mp4")
    with (
        patch.dict(
            "os.environ",
            {
                "MLBB_BANNER_FAST_SHIP_MIN_VOD_SEC": "480",
                "MLBB_BANNER_FAST_SHIP_MIN_PEAK_SEC": "240",
                "MLBB_VOD_MIN_PEAK_SEC": "300",
                "MLBB_BANNER_FAST_SHIP_MIN_TIER": "3",
            },
            clear=False,
        ),
        patch("mlbb_vod_segment_feed._ffprobe_duration", return_value=180.0),
    ):
        ok, reason = _banner_fast_ship_seed_ok(vod, 280.0, tier=3)
        assert not ok
        assert "vod_too_short" in reason

    with (
        patch.dict(
            "os.environ",
            {
                "MLBB_BANNER_FAST_SHIP_MIN_VOD_SEC": "480",
                "MLBB_BANNER_FAST_SHIP_MIN_PEAK_SEC": "240",
                "MLBB_BANNER_FAST_SHIP_MIN_TIER": "3",
            },
            clear=False,
        ),
        patch("mlbb_vod_segment_feed._ffprobe_duration", return_value=900.0),
    ):
        ok_early, reason_early = _banner_fast_ship_seed_ok(vod, 138.0, tier=3)
        assert not ok_early
        assert "peak_too_early" in reason_early
        ok_double, reason_double = _banner_fast_ship_seed_ok(vod, 285.0, tier=2)
        assert not ok_double
        assert "tier_low" in reason_double
        ok_mid, _ = _banner_fast_ship_seed_ok(vod, 285.0, tier=3)
        assert ok_mid

def test_quality_first_pick_min_rejects_highlight_shorts() -> None:
    from mlbb_vod_segment_feed import _vod_pick_min_sec

    with patch.dict(
        "os.environ",
        {"MLBB_VOD_QUALITY_FIRST": "1", "MLBB_VOD_MIN_SEC": "180", "MLBB_VOD_QUALITY_MIN_SEC": "480"},
        clear=False,
    ):
        assert _vod_pick_min_sec() == 480.0
    with patch.dict(
        "os.environ",
        {"MLBB_VOD_QUALITY_FIRST": "0", "MLBB_VOD_MIN_SEC": "180"},
        clear=False,
    ):
        assert _vod_pick_min_sec() == 180.0
