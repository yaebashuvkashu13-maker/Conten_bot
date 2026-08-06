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
    monkeypatch.setenv("SHOOTER_VOD_DENSE_GUN_MIN", "0.04")
    monkeypatch.setenv("SHOOTER_VOD_DENSE_PROBE_STEP_SEC", "50")
    monkeypatch.setenv("SHOOTER_VOD_DENSE_PROBE_MAX", "20")
    monkeypatch.setenv("HIGHLIGHT_WINDOW_SEC", "10")

    # Synthetic gun peaks at probe offsets 200, 250, 400, 600
    def fake_panns(_path, t, _dur):
        gun = 0.0
        # Match both probe starts and snap sample windows near centers.
        for anchor in (200.0, 250.0, 400.0, 600.0):
            if abs(float(t) - anchor) < 6 or abs(float(t) - (anchor + 5)) < 6:
                gun = 0.55 if abs(float(t) - 600) < 8 else 0.45
                break
        return {"panns_gun_max": gun}

    def fake_snap(path, approx_center, *, duration, **_kw):
        # Keep centers near the probe centers we seeded.
        return round(float(approx_center), 1), 0.08, 0.5

    with patch.object(fast, "score_panns_audio", side_effect=fake_panns):
        with patch.object(fast, "snap_peak_to_gunfire", side_effect=fake_snap):
            with patch("smart_video_editor.ffprobe_duration", return_value=900.0):
                with patch.object(
                    fast, "_dense_offsets", return_value=[200.0, 250.0, 400.0, 600.0]
                ):
                    peaks, reason = fast.discover_montage_gun_peaks(
                        Path("/tmp/x.mp4"), "standoff", min_clips=3, gap_sec=55.0
                    )
    assert len(peaks) >= 3
    assert "picked=" in reason
    # 200 and 250 centers are within 55s — only one of them after spacing
    centers_near = [p for p in peaks if 195 <= p <= 260]
    assert len(centers_near) <= 1


def test_skip_intro_standoff_shorter_than_pubg(monkeypatch) -> None:
    monkeypatch.setenv("PUBG_METRO_VOD_SKIP_INTRO_SEC", "120")
    monkeypatch.setenv("SHOOTER_VOD_FAST_SKIP_INTRO", "60")
    assert fast._skip_intro_sec("pubg") == 120.0
    assert fast._skip_intro_sec("standoff") == 60.0
