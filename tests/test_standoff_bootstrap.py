"""Standoff cold-start bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from standoff_vod_bootstrap import BOOTSTRAP_ENV, standoff_bootstrap_active  # noqa: E402
from vod_peak_gap import segment_gap_sec  # noqa: E402
from vod_scan_state import peaks_near_sent_reason  # noqa: E402


def test_standoff_bootstrap_env_keys() -> None:
    assert "PUBG_PRESEND_COMBAT_FAST" in BOOTSTRAP_ENV
    assert float(BOOTSTRAP_ENV["SMART_STANDOFF_MIN_BURST_RATIO"]) < 8.0


def test_shooter_soft_gap_at_l1() -> None:
    import os

    os.environ["SHOOTER_VOD_SEGMENT_GAP_SEC"] = "45"
    os.environ["SHOOTER_VOD_SOFT_SEGMENT_GAP_SEC"] = "7"
    assert segment_gap_sec("standoff", soften_level=1) == 7.0


def test_peaks_near_sent_requires_sent_peaks() -> None:
    assert peaks_near_sent_reason({"reject_reason": "peaks_near_sent pool=4 sent=[]"}) is False
    assert peaks_near_sent_reason(
        {"reject_reason": "peaks_near_sent pool=4 sent=[81.0]", "last_sent_peaks": [81.0]}
    ) is True
