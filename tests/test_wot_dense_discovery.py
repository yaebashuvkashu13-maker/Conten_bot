#!/usr/bin/env python3
"""Unit tests for WoT dense discovery helpers (no ffmpeg required)."""

from __future__ import annotations

import os

import pytest


def test_cluster_seed_times_spreads_peaks():
    from wot_vod_fast_scan import _cluster_seed_times

    scored = [(0.9, 100.0), (0.8, 105.0), (0.7, 200.0), (0.6, 210.0), (0.5, 400.0)]
    seeds = _cluster_seed_times(scored, gap_sec=40.0, top_k=5)
    assert seeds == [100.0, 200.0, 400.0]


def test_apply_fast_probe_seeds_merge():
    from wot_vod_fast_scan import apply_fast_probe_seeds, clear_fast_probe_seeds

    clear_fast_probe_seeds()
    os.environ.pop("HIGHLIGHT_SEED_MERGE", None)
    apply_fast_probe_seeds([120.0, 240.5])
    assert os.environ.get("HIGHLIGHT_ALLOW_SEED_STARTS") == "1"
    assert os.environ.get("HIGHLIGHT_SEED_MERGE") == "1"
    assert "120.0" in os.environ.get("HIGHLIGHT_SEED_STARTS", "")
    clear_fast_probe_seeds()
    assert "HIGHLIGHT_SEED_STARTS" not in os.environ
    assert "HIGHLIGHT_SEED_MERGE" not in os.environ


def test_expand_disabled_passthrough(tmp_path, monkeypatch):
    from wot_brawl_fight import expand_clip_to_full_brawl

    monkeypatch.setenv("WOT_BRAWL_FULL_FIGHT", "0")
    monkeypatch.setenv("SHOOTER_VOD_VARIABLE_LENGTH", "0")
    clip = {"start": 50.0, "peak_start": 55.0, "input_duration": 10.0}
    out = expand_clip_to_full_brawl(tmp_path / "missing.mp4", clip)
    assert out == clip


def test_run_containing_peak_gap_tolerance():
    from wot_brawl_fight import _run_containing_peak

    # Active, gap, active — should stay one run with tolerate=3
    series = [
        (10.0, 0.01, 0.03),
        (11.0, 0.01, 0.03),
        (12.0, 0.0, 0.0),  # gap
        (13.0, 0.0, 0.0),
        (14.0, 0.01, 0.03),
        (15.0, 0.01, 0.03),
    ]
    run = _run_containing_peak(series, peak=12.5)
    assert run is not None
    assert run[0] == 10.0
    assert run[1] >= 14.0
