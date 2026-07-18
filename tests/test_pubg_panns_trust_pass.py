"""PANNs-trusted gunfire must pass shooting gate (not die as no_shots)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_shooting_gate import pubg_passes_shooting_gate  # noqa: E402


def test_panns_trust_passes_despite_low_density(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PUBG_PANNS_TRUST_MIN", "0.35")
    monkeypatch.setenv("SMART_PUBG_MIN_GUNFIRE_DENSITY", "0.068")
    vod = tmp_path / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"x")
    metrics = {
        "start": 94.0,
        "duration": 15.0,
        "gunfire_density": 0.034,
        "burst_ratio": 10.4,
        "audio_rms": 0.02,
        "center_motion": 0.05,
        "center_text": 0.05,
        "crop_box": [0, 0, 100, 100],
    }
    with patch("pubg_shooting_gate.pubg_probe_segment", return_value=metrics), patch(
        "pubg_shooting_gate.segment_looks_like_pubg_loot_or_walk", return_value=False
    ), patch(
        "pubg_shooting_gate.segment_is_valid_for_montage", return_value=(True, "ok")
    ):
        ok, reason, row = pubg_passes_shooting_gate(vod, 94.0, 15.0, panns_gun_max=0.682)
    assert ok is True, reason
    assert "panns_trust" in reason or "panns" in reason
    assert row.get("owner_reason", "").startswith("panns_trust")
