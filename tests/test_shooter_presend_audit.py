"""Presend audit and tightened PUBG combat pool gates."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_send_video_prefers_inline_for_shooter(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VOD_CALIBRATION_SEND_AS_FILE", "1")
    monkeypatch.setenv("SHOOTER_VOD_SEND_AS_VIDEO", "1")
    monkeypatch.setenv("DAILY_GAME_CYCLE_ENABLED", "0")

    from mlbb_vod_segment_feed import send_video

    path = tmp_path / "seg.mp4"
    path.write_bytes(b"\x00" * 5_000_000)
    with (
        patch("mlbb_learning_first.can_send", return_value=(True, "")),
        patch("mlbb_learning_first.record_send"),
        patch("mlbb_telegram_video.send_hq_files") as hq,
        patch("mlbb_telegram_video.send_video_file", return_value=True) as vid,
        patch("mlbb_telegram_video.compress_for_inline_video", side_effect=lambda p, **k: (p, False)),
    ):
        ok = send_video("tok", "chat", path, "cap", seg_id="x", cycle_game="pubg")
    assert ok is True
    vid.assert_called_once()
    hq.assert_not_called()


def test_scan_fast_requires_visual_not_audio_only(monkeypatch) -> None:
    monkeypatch.setenv("PUBG_PANNS_TRUST_MIN", "0.35")

    from pubg_combat_gate import pubg_passes_combat_gate

    vod = Path("/tmp/vod.mp4")
    with (
        patch("highlight_scorer.score_panns_audio", return_value={"panns_gun_max": 0.60}),
        patch("highlight_scorer.calibrated_pann_gun_min", return_value=0.22),
        patch(
            "pubg_combat_gate.pubg_combat_visual_fast",
            return_value=(False, "no_combat_signal", {}),
        ),
    ):
        ok, reason, _ = pubg_passes_combat_gate(vod, 100.0, 15.0, "pubg", scan_fast=True)
    assert ok is False
    assert "no_combat" in reason or "combat" in reason


def test_presend_audit_rejects_weak_gunfire(monkeypatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_PRESEND_AUDIT", "1")
    monkeypatch.setenv("PUBG_PRESEND_MIN_GUN_DENSITY", "0.040")
    monkeypatch.setenv("PUBG_PRESEND_MIN_BURST", "3.5")

    from shooter_vod_presend_audit import audit_pubg_segment

    rendered = Path("/tmp/rendered.mp4")
    with (
        patch("mlbb_vod_segment_feed._ffprobe_duration", return_value=10.0),
        patch(
            "pubg_combat_gate.pubg_passes_combat_gate",
            return_value=(True, "combat_visual_strict", {"panns_gun_max": 0.40}),
        ),
        patch(
            "pubg_shooting_gate.pubg_probe_segment",
            return_value={
                "gunfire_density": 0.005,
                "burst_ratio": 1.0,
                "center_motion": 0.01,
            },
        ),
        patch(
            "pubg_killfeed_ocr.score_killfeed_segment",
            return_value=(0.0, {"killfeed_hits": []}),
        ),
    ):
        ok, reason, _ = audit_pubg_segment(rendered)
    assert ok is False
    assert "weak_combat" in reason
