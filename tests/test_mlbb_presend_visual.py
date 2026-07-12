"""Presend visual bypass for verified kill-banner clips."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_vod_segment_feed import (  # noqa: E402
    _presend_visual_ok,
    _verified_discovery_banner,
)


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


def test_presend_reuses_verified_discovery_banner(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_BANNER_SEND_STRICT", "0")
    monkeypatch.setenv("MLBB_VOD_PRESEND_FAST_BANNER", "1")
    ok, reason = _verified_discovery_banner(
        {
            "kill_banner": "savage",
            "kill_banner_tier": 5,
            "banner_sec": 616.0,
        },
        5,
    )
    assert ok is True
    assert reason == "verified_discovery_banner:savage@616.0s"


def test_presend_does_not_trust_below_base_tier(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_BANNER_SEND_STRICT", "0")
    monkeypatch.setenv("MLBB_VOD_PRESEND_FAST_BANNER", "1")
    monkeypatch.setenv("MLBB_KILL_BANNER_MIN_TIER", "double")
    ok, reason = _verified_discovery_banner(
        {"kill_banner": "single", "kill_banner_tier": 1, "banner_sec": 50},
        5,
    )
    assert ok is False
    assert reason == ""


def test_presend_trusts_double_when_title_min_higher(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_BANNER_SEND_STRICT", "0")
    monkeypatch.setenv("MLBB_VOD_PRESEND_FAST_BANNER", "1")
    monkeypatch.setenv("MLBB_KILL_BANNER_MIN_TIER", "double")
    ok, reason = _verified_discovery_banner(
        {"kill_banner": "double", "kill_banner_tier": 2, "banner_sec": 50},
        5,
    )
    assert ok is True
    assert reason == "verified_discovery_banner:double@50.0s"


def test_strict_banner_mode_requires_fresh_visual_proof(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_BANNER_SEND_STRICT", "1")
    ok, reason = _verified_discovery_banner(
        {"kill_banner": "savage", "kill_banner_tier": 5, "banner_sec": 50},
        5,
    )
    assert ok is False
    assert reason == ""
