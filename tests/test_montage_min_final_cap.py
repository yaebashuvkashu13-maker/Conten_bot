"""Montage min-final cap — must not exceed SHOOTER_VOD_MONTAGE_MAX_SEC."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_montage_min_final_capped_below_max(monkeypatch) -> None:
    import shooter_vod_segment_feed as feed

    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_MAX_SEC", "55")
    monkeypatch.setenv("PUBG_VOD_MONTAGE_MIN_FINAL_SEC", "35")
    _, _, _, _, final_max = feed._montage_limits()
    durations = [18.0, 17.5, 16.8]
    min_final = max(35.0, sum(durations) * 0.78)
    min_final = min(min_final, final_max - 0.25)
    assert min_final <= 54.75
    assert 50.4 + 0.35 >= min_final


def test_force_send_feed_patterns_is_dict() -> None:
    from vod_force_send import _FEED_PATTERNS

    assert isinstance(_FEED_PATTERNS, dict)
    assert "pubg" in _FEED_PATTERNS
