"""Owner calibration peaks are independent — nearby labels must not block each other."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from shooter_vod_segment_feed import _owner_peak_already_sent  # noqa: E402
from vod_scan_state import shooter_interval_blocked, shooter_peak_fight_blocked  # noqa: E402


def test_owner_nearby_peaks_not_fight_blocked() -> None:
    """kFZA1C3Ze4s labels 81s and 90s — 9s apart, both must remain available."""
    used = [81.0]
    assert shooter_peak_fight_blocked(90.0, used, game="pubg", soften_gap=7.0) is True
    assert _owner_peak_already_sent(90.0, used) is False
    assert _owner_peak_already_sent(81.0, used) is True


def test_owner_overlapping_timeline_not_interval_blocked() -> None:
    """Sent clip 77-122 must not block owner cut at 90 (86-131) — user labeled both."""
    sent_interval = [(77.0, 122.0)]
    assert shooter_interval_blocked(86.0, 131.0, sent_interval) is True
