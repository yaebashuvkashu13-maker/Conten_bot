#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_kill_banner import (  # noqa: E402
    _DISCOVERY_CACHE,
    _discovery_cache_key,
    bounds_from_banner,
    classify_banner_text,
    clear_banner_discovery_cache,
)


def test_classify_savage_and_triple() -> None:
    s = classify_banner_text("SAVAGE!")
    assert s is not None
    assert s.tier == 5
    assert s.label == "savage"
    t = classify_banner_text("TRIPLE KILL")
    assert t is not None
    assert t.tier == 3
    assert t.label == "triple"
    m = classify_banner_text("MANIAC")
    assert m is not None
    assert m.tier == 4


def test_reject_single_kill_only() -> None:
    single = classify_banner_text("You got a Kill")
    assert single is not None
    assert single.tier == 1


def test_classify_double_kill() -> None:
    d = classify_banner_text("DOUBLE KILL")
    assert d is not None
    assert d.tier == 2
    assert d.label == "double"
    partial = classify_banner_text("OUBLE KILL")
    assert partial is not None
    assert partial.tier == 2


def test_discovery_cache_key_tracks_source_file(tmp_path: Path) -> None:
    vod = tmp_path / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"first")
    first = _discovery_cache_key(vod, 5, True)
    vod.write_bytes(b"second-version")
    second = _discovery_cache_key(vod, 5, True)
    assert first != second
    _DISCOVERY_CACHE[first] = tuple()
    clear_banner_discovery_cache()
    assert not _DISCOVERY_CACHE


def test_min_tier_double_accepts_double_rejects_single() -> None:
    import mlbb_kill_banner as kb

    old = os.environ.get("MLBB_KILL_BANNER_MIN_TIER")
    os.environ["MLBB_KILL_BANNER_MIN_TIER"] = "double"
    try:
        assert kb._min_tier() == 2
        double = classify_banner_text("DOUBLE KILL")
        assert double is not None
        assert double.tier >= kb._min_tier()
        single = classify_banner_text("You got a Kill")
        assert single is not None
        assert single.tier < kb._min_tier()
    finally:
        if old is None:
            os.environ.pop("MLBB_KILL_BANNER_MIN_TIER", None)
        else:
            os.environ["MLBB_KILL_BANNER_MIN_TIER"] = old


def test_reject_enemy_triple() -> None:
    assert classify_banner_text("Enemy Triple Kill!") is None
    assert classify_banner_text("ENEMY SAVAGE") is None
    t = classify_banner_text("TRIPLE KILL")
    assert t is not None
    assert t.tier == 3


def test_bounds_from_fight_sustain() -> None:
    os.environ["MLBB_VOD_LEAD_SEC"] = "4"
    os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
    os.environ["MLBB_FIGHT_MAX_SEC"] = "28"
    os.environ["MLBB_FIGHT_HARD_MAX_SEC"] = "32"
    start, end, dur = bounds_from_banner(
        100.0,
        file_dur=200.0,
        fight_start=88.0,
        fight_end=118.0,
    )
    assert start == 84.0
    assert end == 116.0
    assert dur == 32.0


def test_bounds_fallback_without_fight() -> None:
    os.environ["MLBB_VOD_LEAD_SEC"] = "4"
    os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
    os.environ["MLBB_FIGHT_MAX_SEC"] = "28"
    start, end, dur = bounds_from_banner(50.0, file_dur=120.0)
    assert start == 46.0
    assert 8.0 <= dur <= 28.0


def test_savage_banner_lead_starts_earlier() -> None:
    os.environ["MLBB_VOD_LEAD_SEC"] = "4"
    os.environ["MLBB_SAVAGE_BANNER_LEAD_SEC"] = "14"
    os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
    os.environ["MLBB_FIGHT_MAX_SEC"] = "28"
    os.environ["MLBB_FIGHT_HARD_MAX_SEC"] = "32"
    start, end, dur = bounds_from_banner(9.0, file_dur=120.0, banner_tier=5)
    assert start == 0.0
    assert dur >= 8.0


def test_discover_banners_handles_numpy_motion() -> None:
    import numpy as np
    import mlbb_kill_banner as kb

    class FakeVod(Path):
        pass

    vod = FakeVod("/tmp/fake_vod.mp4")
    fake_analysis = {
        "duration": 600.0,
        "window_seconds": 2.0,
        "center_motion": np.linspace(0.01, 0.5, 300, dtype=np.float32),
        "audio": np.linspace(0.0, 0.3, 300, dtype=np.float32),
    }

    def fake_analysis_for(_vod: Path) -> dict:
        return fake_analysis

    def fake_scan_window(*_a, **_kw):
        return []

    old = os.environ.get("MLBB_VOD_BANNER_DISCOVER")
    os.environ["MLBB_VOD_BANNER_DISCOVER"] = "1"
    try:
        import mlbb_fight_segment as fight

        orig = fight._analysis_for
        orig_scan = kb.scan_window
        fight._analysis_for = fake_analysis_for
        kb.scan_window = fake_scan_window
        try:
            hits = kb.discover_vod_kill_banners(vod)
            assert hits == []
        finally:
            fight._analysis_for = orig
            kb.scan_window = orig_scan
    finally:
        if old is None:
            os.environ.pop("MLBB_VOD_BANNER_DISCOVER", None)
        else:
            os.environ["MLBB_VOD_BANNER_DISCOVER"] = old

    os.environ["MLBB_VOD_LEAD_SEC"] = "4"
    os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
    os.environ["MLBB_FIGHT_MAX_SEC"] = "28"
    os.environ["MLBB_FIGHT_HARD_MAX_SEC"] = "32"
    # Banner at 27s in a 28s window ending at 28 — was tail-heavy.
    start, end, dur = bounds_from_banner(
        27.0,
        file_dur=120.0,
        fight_start=0.0,
        fight_end=28.0,
    )
    rel = (27.0 - start) / dur
    assert rel <= 0.58


def test_resolve_fight_bounds_motion_when_motion_anchor_ok() -> None:
    import mlbb_kill_banner as kb
    from unittest.mock import patch

    vod = Path("/tmp/fake_vod_motion.mp4")
    old = {
        k: os.environ.get(k)
        for k in (
            "MLBB_VOD_KILL_BANNER",
            "MLBB_KILL_BANNER_REQUIRED",
            "MLBB_VOD_BANNER_PRESEND",
            "MLBB_VOD_MOTION_ANCHOR_OK",
        )
    }
    os.environ["MLBB_VOD_KILL_BANNER"] = "1"
    os.environ["MLBB_KILL_BANNER_REQUIRED"] = "1"
    os.environ["MLBB_VOD_BANNER_PRESEND"] = "1"
    os.environ["MLBB_VOD_MOTION_ANCHOR_OK"] = "1"
    try:
        with (
            patch.object(kb, "find_banner_near_peak", return_value=None),
            patch("mlbb_fight_segment.detect_fight_bounds", return_value=(90.0, 118.0, 28.0)),
        ):
            out = kb.resolve_fight_bounds(vod, 100.0, 600.0)
        assert out is not None
        start, end, dur, meta = out
        assert meta["anchor"] == "motion"
        assert dur >= 8.0
    finally:
        for key, val in old.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def test_resolve_fight_bounds_strict_requires_banner() -> None:
    import mlbb_kill_banner as kb
    from unittest.mock import patch

    vod = Path("/tmp/fake_vod_strict.mp4")
    old = {
        k: os.environ.get(k)
        for k in ("MLBB_VOD_KILL_BANNER", "MLBB_KILL_BANNER_REQUIRED", "MLBB_VOD_MOTION_ANCHOR_OK")
    }
    os.environ["MLBB_VOD_KILL_BANNER"] = "1"
    os.environ["MLBB_KILL_BANNER_REQUIRED"] = "1"
    os.environ.pop("MLBB_VOD_MOTION_ANCHOR_OK", None)
    os.environ.pop("MLBB_VOD_BANNER_PRESEND", None)
    try:
        with (
            patch.object(kb, "find_banner_near_peak", return_value=None),
            patch("mlbb_fight_segment.detect_fight_bounds", return_value=(90.0, 118.0, 28.0)),
        ):
            assert kb.resolve_fight_bounds(vod, 100.0, 600.0) is None
    finally:
        for key, val in old.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def test_resolve_fight_bounds_tries_deep_scan_before_reject() -> None:
    import mlbb_kill_banner as kb
    from unittest.mock import patch

    vod = Path("/tmp/fake_vod_deep.mp4")
    hit = kb.KillBannerHit(sec=102.0, tier=2, label="double", text="DOUBLE KILL", source="ocr")
    calls: list[bool] = []

    def fake_find(_vod, _peak, *, quick: bool = False):
        calls.append(quick)
        return hit if not quick else None

    old = {
        k: os.environ.get(k)
        for k in ("MLBB_VOD_KILL_BANNER", "MLBB_KILL_BANNER_REQUIRED", "MLBB_KILL_BANNER_MIN_TIER")
    }
    os.environ["MLBB_VOD_KILL_BANNER"] = "1"
    os.environ["MLBB_KILL_BANNER_REQUIRED"] = "1"
    os.environ["MLBB_KILL_BANNER_MIN_TIER"] = "double"
    os.environ.pop("MLBB_VOD_MOTION_ANCHOR_OK", None)
    try:
        with (
            patch.object(kb, "find_banner_near_peak", side_effect=fake_find),
            patch("mlbb_fight_segment.detect_fight_bounds", return_value=(88.0, 116.0, 28.0)),
        ):
            out = kb.resolve_fight_bounds(vod, 100.0, 600.0)
        assert calls == [True, False]
        assert out is not None
        assert out[3]["anchor"] == "kill_banner"
    finally:
        for key, val in old.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
