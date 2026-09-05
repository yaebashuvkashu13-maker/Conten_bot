#!/usr/bin/env python3
import json
import os
from pathlib import Path

import numpy as np
import pytest


def test_cascade_limits_defaults(monkeypatch):
    monkeypatch.delenv("VOD_CASCADE_PANN_MAX", raising=False)
    monkeypatch.delenv("VOD_CASCADE_FAST_RANKER_MAX", raising=False)
    monkeypatch.delenv("VOD_CASCADE_KILL_MAX", raising=False)
    monkeypatch.setenv("PUBG_FULL_PEAK_SCAN", "1")
    from vod_scan_cascade import cascade_limits, full_peak_scan_enabled

    assert full_peak_scan_enabled() is True
    limits = cascade_limits()
    # Full scan: stage caps are unlimited (0).
    assert limits.panns == 0
    assert limits.fast_ranker == 0
    assert limits.kill_notification == 0


def test_apply_cascade_to_pool(monkeypatch):
    peaks = [10.0, 20.0, 30.0, 40.0, 50.0]
    monkeypatch.setenv("PUBG_FULL_PEAK_SCAN", "1")
    from vod_scan_cascade import apply_cascade_to_pool

    # Full scan must not truncate to top-8.
    assert apply_cascade_to_pool(peaks, "kill") == peaks
    assert apply_cascade_to_pool(peaks, "panns") == peaks

    monkeypatch.setenv("PUBG_FULL_PEAK_SCAN", "0")
    monkeypatch.setenv("VOD_CASCADE_PANN_MAX", "8")
    monkeypatch.setenv("VOD_CASCADE_KILL_MAX", "8")
    import importlib
    import vod_scan_cascade as vsc

    importlib.reload(vsc)
    assert vsc.apply_cascade_to_pool(peaks, "panns") == peaks[:8]
    assert vsc.apply_cascade_to_pool(peaks, "kill") == peaks[:8]


def test_full_scan_zero_cap_must_not_empty_slice(monkeypatch):
    """Regression: scored[:limits.fast_ranker] with cap=0 emptied the pool."""
    monkeypatch.setenv("PUBG_FULL_PEAK_SCAN", "1")
    from vod_scan_cascade import cascade_limits, apply_cascade_to_pool, full_peak_scan_enabled

    peaks = [float(i) for i in range(40)]
    limits = cascade_limits()
    assert full_peak_scan_enabled()
    assert limits.fast_ranker == 0
    assert limits.kill_notification == 0
    # Correct API keeps the list; naive [:0] would drop everything.
    assert apply_cascade_to_pool(peaks, "fast_ranker") == peaks
    assert apply_cascade_to_pool(peaks, "kill") == peaks
    assert peaks[: limits.fast_ranker] == []  # documents the footgun


def test_runtime_labels_seed_from_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    data = repo / "data"
    data.mkdir(parents=True)
    seed = data / "pubg_owner_labels.json"
    seed.write_text(json.dumps({"videos": {"abc": [{"time_sec": 1, "label": "good"}]}}))
    runtime = tmp_path / "runtime" / "pubg_owner_labels.json"
    monkeypatch.setenv("CONTENT_BOT_REPO", str(repo))
    monkeypatch.setenv("PUBG_OWNER_LABELS_PATH", str(runtime))
    from runtime_labels import ensure_runtime_labels, load_runtime_labels

    path = ensure_runtime_labels("pubg")
    assert path == runtime
    loaded = load_runtime_labels("pubg")
    assert "abc" in loaded["videos"]


def test_ranked_pool_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("VOD_RANKED_POOL_CACHE_DIR", str(tmp_path))
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"x" * 64)
    from vod_ranked_pool_cache import get_ranked_pool, put_ranked_pool, unused_peaks

    put_ranked_pool(vod, ranked_peaks=[100.0, 200.0, 300.0], reason="test")
    hit = get_ranked_pool(vod)
    assert hit is not None
    assert hit["ranked_peaks"] == [100.0, 200.0, 300.0]
    rest = unused_peaks(vod, used=[100.0], gap_sec=30.0)
    assert 200.0 in rest and 100.0 not in rest


def test_feature_store_pcm_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("VOD_FEATURE_STORE_DIR", str(tmp_path / "store"))
    vod = tmp_path / "vod.mp4"
    vod.write_bytes(b"y" * 128)
    from vod_feature_store import VodFeatureStore

    store = VodFeatureStore(vod, skip_intro=0.0)
    pcm = np.array([100, -100, 200, -200], dtype=np.int16)
    pcm_file = tmp_path / "store" / "pcm" / f"{store.key}.s16le"
    pcm_file.parent.mkdir(parents=True, exist_ok=True)
    pcm_file.write_bytes(pcm.tobytes())
    meta_file = tmp_path / "store" / "meta" / f"{store.key}.json"
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    meta_file.write_text(
        json.dumps(
            {
                "version": 1,
                "saved_at": 9_999_999_999,
                "pcm_duration_sec": 0.01,
                "features": {"gun_density": 0.5},
            }
        )
    )
    loaded = store.get_pcm_s16()
    assert loaded.size == 4
    assert store.get_feature("gun_density") == 0.5


def test_champion_compare_rejects_bad_accept():
    from pubg_ranker_champion import compare_benchmark

    ok, reasons = compare_benchmark(
        {"bad_accepted_hits": 1, "good_accepted_rate": 0.5, "approved_clips_per_min": 2.0},
        {"bad_accepted_hits": 2, "good_accepted_rate": 0.5, "approved_clips_per_min": 2.0},
    )
    assert not ok
    assert any("bad_accept" in r for r in reasons)


def test_event_dedup_merge():
    from vod_event_dedup import merge_nearby_peaks

    peaks = merge_nearby_peaks([100.0, 110.0, 200.0], merge_gap_sec=25.0)
    assert peaks == [100.0, 200.0]


def test_youtube_source_health_block(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_SOURCE_HEALTH_PATH", str(tmp_path / "health.json"))
    from youtube_source_health import is_blocked, record_download_result

    for _ in range(5):
        record_download_result(url="bad-channel", ok=False, error_kind="auth")
    blocked, _ = is_blocked(url="bad-channel")
    assert blocked is True


def test_config_status_ok(monkeypatch):
    monkeypatch.setenv("PUBG_KILL_NOTIFICATION_MODE", "prefer")
    monkeypatch.delenv("PUBG_REQUIRE_KILL_NOTIFICATION", raising=False)
    from vod_config import config_status

    status = config_status()
    assert status["ok"] is True
    assert "cascade" in status
