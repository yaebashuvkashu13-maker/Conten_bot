"""Tests for dense gun-peak discovery used by fast montage path."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import shooter_vod_fast_scan as fast  # noqa: E402


def test_dense_offsets_caps_and_steps(monkeypatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_DENSE_PROBE_STEP_SEC", "60")
    monkeypatch.setenv("SHOOTER_VOD_DENSE_PROBE_MAX", "10")
    offs = fast._dense_offsets(2000.0, skip_intro=120.0)
    assert len(offs) == 10
    assert offs[0] == 120.0
    assert offs[1] - offs[0] == 60.0


def test_discover_montage_picks_spaced_peaks(monkeypatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_DENSE_PANN_MIN", "0.16")
    monkeypatch.setenv("SHOOTER_VOD_DENSE_PROBE_STEP_SEC", "50")
    monkeypatch.setenv("SHOOTER_VOD_DENSE_PROBE_MAX", "20")

    # Synthetic gun peaks at 200, 280 (too close), 400, 600
    def fake_panns(_path, t, _dur):
        gun = 0.0
        if abs(t - 200) < 1:
            gun = 0.5
        elif abs(t - 250) < 1:
            gun = 0.4
        elif abs(t - 400) < 1:
            gun = 0.45
        elif abs(t - 600) < 1:
            gun = 0.55
        return {"panns_gun_max": gun}

    with patch.object(fast, "score_panns_audio", side_effect=fake_panns):
        with patch("smart_video_editor.ffprobe_duration", return_value=900.0):
            # Force offsets to include our peaks
            with patch.object(fast, "_dense_offsets", return_value=[200.0, 250.0, 400.0, 600.0]):
                peaks, reason = fast.discover_montage_gun_peaks(
                    Path("/tmp/x.mp4"), "standoff", min_clips=3, gap_sec=55.0
                )
    assert len(peaks) >= 3
    assert "picked=3" in reason or "picked=4" in reason
    # 200 and 250 are within 55s — only one of them
    assert not (200.0 in peaks and 250.0 in peaks)
