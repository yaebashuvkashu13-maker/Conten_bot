"""Production PUBG feed must autodetect fights — owner labels are training only."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from shooter_vod_segment_feed import _owner_label_pool_clips  # noqa: E402


def test_owner_label_pool_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("SHOOTER_VOD_OWNER_LABEL_CUTS", raising=False)
    with patch("pubg_owner_calibration.has_owner_labels", return_value=True), patch(
        "pubg_owner_calibration.labels_for_video",
        return_value=[{"time_sec": 81.0, "label": "good"}],
    ):
        pool = _owner_label_pool_clips(
            Path("yt_kFZA1C3Ze4s.mp4"),
            "pubg",
            "kFZA1C3Ze4s",
            blocked_ids=set(),
            used_peaks=[],
            seg_gap=30.0,
        )
    assert pool == []


def test_owner_label_pool_only_when_explicitly_enabled(monkeypatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_OWNER_LABEL_CUTS", "1")
    with patch("pubg_owner_calibration.has_owner_labels", return_value=True), patch(
        "pubg_owner_calibration.labels_for_video",
        return_value=[{"time_sec": 81.0, "label": "good"}],
    ):
        pool = _owner_label_pool_clips(
            Path("yt_kFZA1C3Ze4s.mp4"),
            "pubg",
            "kFZA1C3Ze4s",
            blocked_ids=set(),
            used_peaks=[],
            seg_gap=30.0,
        )
    assert len(pool) == 1
    assert pool[0]["owner_label_cut"] is True
