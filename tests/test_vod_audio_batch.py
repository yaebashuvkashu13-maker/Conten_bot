"""Tests for batch audio DSP, feature cache, scan funnel."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _synthetic_gun_pcm(duration_sec: float = 10.0, sample_rate: int = 11025) -> np.ndarray:
    n = int(duration_sec * sample_rate)
    pcm = np.zeros(n, dtype=np.float32)
    for spike_at in (2.0, 5.0, 8.0):
        idx = int(spike_at * sample_rate)
        pcm[idx : idx + 128] = 0.9
    return pcm


def test_gunfire_metrics_from_pcm_detects_spikes() -> None:
    from vod_audio_batch import gunfire_metrics_from_pcm

    pcm = _synthetic_gun_pcm()
    density, burst, rms = gunfire_metrics_from_pcm(pcm, 11025, 0.0, 10.0, pcm_base_sec=0.0)
    assert density > 0.005
    assert burst >= 1.0
    assert rms > 0.0


def test_dsp_score_offsets_without_ffmpeg() -> None:
    from vod_audio_batch import dsp_score_offsets

    pcm = _synthetic_gun_pcm()
    rows = dsp_score_offsets([1.0, 4.0, 7.0], 4.0, pcm, pcm_base_sec=0.0)
    assert len(rows) == 3
    assert max(r[1] for r in rows) > 0.0


def test_peak_feature_cache_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vod = tmp_path / "yt_test1234567.mp4"
    vod.write_bytes(b"fake")
    cache_dir = tmp_path / "feat"
    monkeypatch.setenv("VOD_PEAK_FEATURE_CACHE_DIR", str(cache_dir))

    from vod_peak_feature_cache import get_cached, put_cached

    put_cached(vod, 0, peaks=[100.0, 200.0], reason="test", funnel={"picked": 2})
    hit = get_cached(vod, 0)
    assert hit is not None
    assert hit["peaks"] == [100.0, 200.0]
    assert hit["funnel"]["picked"] == 2


def test_scan_funnel_summary() -> None:
    from vod_scan_funnel import ScanFunnel

    f = ScanFunnel(offsets_probed=32, dsp_pass=18, panns_pass=9, picked=5, sent=1)
    assert "probe=32" in f.summary()
    assert "send=1" in f.summary()
    d = f.to_dict()
    assert d["picked"] == 5


def test_discover_montage_uses_feature_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vod = tmp_path / "yt_abc123xyz00.mp4"
    vod.write_bytes(b"x")
    cache_dir = tmp_path / "feat"
    monkeypatch.setenv("VOD_PEAK_FEATURE_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("SHOOTER_VOD_AUDIO_BATCH", "0")

    from vod_peak_feature_cache import put_cached
    from shooter_vod_fast_scan import discover_montage_gun_peaks
    from vod_scan_funnel import ScanFunnel

    put_cached(vod, 0, peaks=[120.0, 240.0, 360.0], reason="cached_test")
    funnel = ScanFunnel()
    peaks, reason = discover_montage_gun_peaks(vod, "pubg", probe_pass=0, funnel=funnel)
    assert len(peaks) == 3
    assert "feature_cache" in reason
    assert funnel.feature_cache_hit is True
