"""Owner calibration cuts — exact timestamp, pinned start."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pubg_fight_segment import apply_fight_bounds_to_clip, snap_peak_to_owner_label  # noqa: E402


def test_snap_peak_to_owner_label() -> None:
    vod = Path("yt_YT6I7rkKLW4.mp4")
    labels = {
        "YT6I7rkKLW4": [
            {"time_sec": 91, "label": "good"},
        ]
    }

    def fake_labels(_vod: Path) -> list:
        return labels["YT6I7rkKLW4"]

    with patch.dict("os.environ", {"SHOOTER_VOD_OWNER_ANCHOR_PEAK": "1"}), patch(
        "pubg_owner_calibration.labels_for_video", side_effect=fake_labels
    ), patch(
        "pubg_owner_calibration.nearest_owner_label",
        return_value=("good", 3.0),
    ):
        peak, snapped = snap_peak_to_owner_label(vod, 94.0)
    assert snapped is True
    assert peak == 91.0


def test_snap_peak_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("SHOOTER_VOD_OWNER_ANCHOR_PEAK", raising=False)
    peak, snapped = snap_peak_to_owner_label(Path("yt_x.mp4"), 94.0)
    assert snapped is False
    assert peak == 94.0


def test_owner_pinned_clip_does_not_expand_start_left() -> None:
    vod = Path("/tmp/vod.mp4")

    def fake_bounds(_vod: Path, peak: float, *, owner_pinned: bool = False):
        if owner_pinned:
            return 87.0, 132.0, 45.0
        return 79.0, 124.0, 45.0

    with patch("pubg_fight_segment.detect_pubg_fight_bounds", side_effect=fake_bounds):
        out = apply_fight_bounds_to_clip(
            {"peak_start": 91.0, "owner_label_cut": True},
            vod,
        )
    assert out["peak_start"] == 91.0
    assert out["start"] == 87.0
