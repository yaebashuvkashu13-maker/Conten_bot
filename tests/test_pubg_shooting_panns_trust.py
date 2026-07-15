"""PUBG shooting gate — PANNs trust must count as allowed owner reason."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_shooting_gate import pubg_passes_shooting_gate  # noqa: E402


def test_panns_trust_owner_reason_passes_when_density_low(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PUBG_PANNS_TRUST_MIN", "0.35")
    monkeypatch.setenv("SMART_PUBG_MIN_GUNFIRE_DENSITY", "0.068")
    monkeypatch.setenv("SMART_PUBG_MIN_BURST_RATIO", "5.2")
    vod = tmp_path / "yt_x.mp4"
    vod.write_bytes(b"")

    metrics = {
        "start": 90.0,
        "duration": 12.0,
        "gunfire_density": 0.01,
        "burst_ratio": 2.5,
        "audio_rms": 0.05,
        "center_motion": 0.04,
        "center_text": 0.1,
        "crop_box": None,
    }
    with patch("pubg_shooting_gate.pubg_probe_segment", return_value=metrics):
        with patch(
            "pubg_owner_calibration.pubg_passes_owner_heuristics",
            return_value=(True, "panns_trust=0.784"),
        ):
            with patch(
                "pubg_shooting_gate.segment_looks_like_pubg_loot_or_walk",
                return_value=False,
            ):
                with patch(
                    "pubg_shooting_gate.segment_is_valid_for_montage",
                    return_value=(True, "ok"),
                ):
                    ok, reason, _ = pubg_passes_shooting_gate(
                        vod, 90.0, 12.0, panns_gun_max=0.784
                    )
    assert ok is True, reason
    assert "panns_trust" in reason
