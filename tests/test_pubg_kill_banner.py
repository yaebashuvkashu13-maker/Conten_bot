"""Tests for PUBG kill-moment OCR discover."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_kill_banner import (  # noqa: E402
    KillMomentHit,
    classify_kill_text,
    dense_scan_enabled,
    discover_vod_kill_moments,
)


def test_classify_headshot_tier2() -> None:
    hit = classify_kill_text("Player1 headshot Player2")
    assert hit is not None
    assert hit.tier >= 2
    assert hit.label == "headshot"


def test_classify_eliminated_tier1() -> None:
    hit = classify_kill_text("Enemy eliminated")
    assert hit is not None
    assert hit.tier == 1
    assert hit.label == "eliminated"


def test_discover_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PUBG_VOD_KILL_DISCOVER", "0")
    vod = tmp_path / "yt_x.mp4"
    vod.write_bytes(b"")
    assert discover_vod_kill_moments(vod, hint_peaks=[120.0]) == []


def test_classify_empty() -> None:
    assert classify_kill_text("") is None
    assert classify_kill_text("lobby waiting") is None


def test_dense_scan_enabled(monkeypatch) -> None:
    monkeypatch.setenv("PUBG_VOD_KILL_DENSE_SEC", "1")
    assert dense_scan_enabled() is True
    monkeypatch.setenv("PUBG_VOD_KILL_DENSE_SEC", "0")
    assert dense_scan_enabled() is False


def test_dense_discover_uses_1hz_batches(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PUBG_VOD_KILL_DISCOVER", "1")
    monkeypatch.setenv("PUBG_VOD_KILL_DENSE_SEC", "1")
    monkeypatch.setenv("PUBG_KILL_DISCOVER_MAX_PROBES", "8")
    monkeypatch.setenv("PUBG_KILL_DISCOVER_MAX_SEC", "30")
    monkeypatch.setenv("PUBG_KILL_DENSE_STOP_ON_HITS", "2")
    monkeypatch.setenv("PUBG_KILL_DENSE_MAX_SPAN_SEC", "120")
    monkeypatch.setenv("PUBG_KILL_DISCOVER_PEAK_HINTS", "0")
    vod = tmp_path / "yt_dense.mp4"
    vod.write_bytes(b"")

    fake_frame = object()
    batch = [(31.0, fake_frame), (32.0, fake_frame), (33.0, fake_frame)]

    def _fake_classify(sec: float, _frame) -> KillMomentHit | None:
        if sec >= 32.0:
            return KillMomentHit(sec=sec, tier=1, label="eliminated", text="eliminated")
        return None

    with patch("mlbb_fight_segment._analysis_for", return_value={"duration": 400.0}):
        with patch("mlbb_kill_banner._ffmpeg_sample_frames", return_value=batch) as sample:
            with patch("pubg_kill_banner._classify_frame", side_effect=_fake_classify):
                with patch("pubg_kill_banner.find_kill_near_peak", return_value=None):
                    hits = discover_vod_kill_moments(vod, hint_peaks=[])

    assert sample.called
    assert len(hits) >= 1
    assert hits[0].label == "eliminated"
