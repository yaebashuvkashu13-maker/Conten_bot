"""Banner fast-ship rows must not crash caption/index on missing score keys."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    hit = MagicMock()
    hit.sec = 253.5
    hit.label = "double"
    hit.tier = 2
    hit.source = "ref"
    vod = Path("/tmp/fake_vod.mp4")

    with (
        patch("mlbb_vod_segment_feed._ffprobe_duration", return_value=900.0),
        patch("mlbb_kill_banner.find_banner_near_peak", return_value=hit) as requick,
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
    assert 253.5 - float(clip["start"]) >= 10.0
    requick.assert_called_once()
    normalize.assert_not_called()
    analysis.assert_not_called()
