#!/usr/bin/env python3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_presend_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("VOD_PRESEND_CACHE_DIR", str(tmp_path))
    vod = tmp_path / "vod.mp4"
    vod.write_bytes(b"z" * 32)
    from vod_presend_cache import get_presend, put_presend

    put_presend(vod, 10.0, 14.0, True, "quality_ok=0.5", {"quality_score": 0.5})
    hit = get_presend(vod, 10.0, 14.0)
    assert hit is not None
    ok, reason, report = hit
    assert ok is True
    assert report["quality_score"] == 0.5


def test_fast_peak_rank_orders_notification_first(monkeypatch):
    monkeypatch.setenv("PUBG_KILL_NOTIFICATION_ENABLED", "1")
    with (
        patch("pubg_shooting_gate.pubg_probe_segment", return_value={"gunfire_density": 0.08, "center_motion": 0.05, "center_text": 0.0}),
        patch("highlight_scorer.score_panns_audio", return_value={"panns_gun_max": 0.4}),
        patch(
            "pubg_kill_notification.score_kill_notification_segment",
            side_effect=[
                (0.05, {"notification_score": 0.05}),
                (0.72, {"notification_score": 0.72}),
            ],
        ),
    ):
        from pubg_fast_peak_rank import rank_peaks_fast

        ranked, reason, meta = rank_peaks_fast(Path("v.mp4"), [100.0, 200.0], "pubg")
    assert ranked[0] == 200.0
    assert "fast_rank" in reason
