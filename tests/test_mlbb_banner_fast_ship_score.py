"""Banner fast-ship rows must not crash caption/index on missing score keys."""

from __future__ import annotations

import sys
from pathlib import Path

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
