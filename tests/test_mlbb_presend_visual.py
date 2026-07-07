"""Presend visual bypass for verified kill-banner clips."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_vod_segment_feed import _presend_visual_ok  # noqa: E402


def test_presend_visual_bypass_menu_on_verified_banner(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_VOD_PRESEND_SKIP_VISUAL_ON_BANNER", "1")
    monkeypatch.setenv("MLBB_PRESEND_MIN_MOTION", "0.012")
    monkeypatch.setenv("MLBB_PRESEND_MIN_MINIMAP_DELTA", "0.010")
    vis = {"visual_pass": False, "fail_reason": "start:menu_overlay"}
    report = {
        "kill_banner": "banner_ok:savage@80.0s",
        "cut_motion": 0.15,
        "peak_motion": 0.14,
        "cut_mini_delta": 0.10,
    }
    row = {"kill_banner_tier": 3}
    ok, reason = _presend_visual_ok(vis, report, row)
    assert ok is True
    assert reason == "visual_banner_bypass"


def test_presend_visual_no_bypass_without_banner(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_VOD_PRESEND_SKIP_VISUAL_ON_BANNER", "1")
    vis = {"visual_pass": False, "fail_reason": "start:menu_overlay"}
    report = {"cut_motion": 0.2, "peak_motion": 0.2, "cut_mini_delta": 0.1}
    row = {}
    ok, reason = _presend_visual_ok(vis, report, row)
    assert ok is False
    assert reason.startswith("visual:")
