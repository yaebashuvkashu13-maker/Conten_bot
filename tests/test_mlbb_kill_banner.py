#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_kill_banner import (  # noqa: E402
    bounds_from_banner,
    classify_banner_text,
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
    # Strong single phrase still accepted at text level.
    strong = classify_banner_text("Enemy has been slain")
    assert strong is not None
    assert strong.tier == 1
    assert strong.label == "single"
    # Bare "kill" is weak — needs announce color in _classify_frame.
    weak = classify_banner_text("You got a Kill")
    assert weak is not None
    assert weak.tier == 1
    assert weak.label == "single_weak"


def test_classify_double_kill() -> None:
    d = classify_banner_text("DOUBLE KILL")
    assert d is not None
    assert d.tier == 2
    assert d.label == "double"
    partial = classify_banner_text("OUBLE KILL")
    assert partial is not None
    assert partial.tier == 2


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
        # Weak label should still be below double min tier.
        assert single.label in {"single", "single_weak"}
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
    os.environ["MLBB_VOD_LEAD_SEC"] = "12"
    os.environ["MLBB_KILL_BANNER_LEAD_SEC"] = "12"
    os.environ.pop("MLBB_BANNER_PRE_SEC", None)
    os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
    os.environ["MLBB_FIGHT_MAX_SEC"] = "40"
    os.environ["MLBB_FIGHT_HARD_MAX_SEC"] = "65"
    start, end, dur = bounds_from_banner(
        100.0,
        file_dur=200.0,
        fight_start=88.0,
        fight_end=118.0,
        banner_tier=2,
    )
    # Lead must pull start before fight_start so prior kills are visible.
    assert start <= 88.0
    assert start <= 100.0 - 12.0
    assert end >= 100.0
    assert dur >= 8.0


def test_bounds_fallback_without_fight() -> None:
    os.environ["MLBB_VOD_LEAD_SEC"] = "12"
    os.environ["MLBB_KILL_BANNER_LEAD_SEC"] = "12"
    os.environ.pop("MLBB_BANNER_PRE_SEC", None)
    os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
    os.environ["MLBB_FIGHT_MAX_SEC"] = "40"
    start, end, dur = bounds_from_banner(50.0, file_dur=120.0, banner_tier=2)
    assert start == 38.0
    assert 8.0 <= dur <= 40.0


def test_bounds_never_puts_banner_at_2_3_seconds() -> None:
    os.environ["MLBB_KILL_BANNER_LEAD_SEC"] = "14"
    os.environ["MLBB_VOD_LEAD_SEC"] = "4"  # legacy short — must not win for banners
    os.environ["MLBB_BANNER_PRE_SEC"] = "2"  # regressive short — must not shrink below lead
    os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
    os.environ["MLBB_FIGHT_MAX_SEC"] = "55"
    os.environ["MLBB_FIGHT_HARD_MAX_SEC"] = "65"
    start, end, dur = bounds_from_banner(
        100.0,
        file_dur=200.0,
        fight_start=98.0,
        fight_end=120.0,
        banner_tier=3,
    )
    banner_at = 100.0 - start
    assert banner_at >= 12.0, f"banner too early at {banner_at:.1f}s"
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

    os.environ["MLBB_KILL_BANNER_LEAD_SEC"] = "12"
    os.environ["MLBB_VOD_LEAD_SEC"] = "12"
    os.environ.pop("MLBB_BANNER_PRE_SEC", None)
    os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
    os.environ["MLBB_FIGHT_MAX_SEC"] = "40"
    os.environ["MLBB_FIGHT_HARD_MAX_SEC"] = "65"
    # Banner near end of fight window — still keep full lead.
    start, end, dur = bounds_from_banner(
        27.0,
        file_dur=120.0,
        fight_start=0.0,
        fight_end=28.0,
        banner_tier=2,
    )
    banner_at = 27.0 - start
    assert banner_at >= 12.0
    # With long lead, banner naturally sits later in the clip — that is OK.
    assert end > 27.0


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


def test_banner_hit_source_ok_accepts_ref() -> None:
    import mlbb_kill_banner as kb

    assert kb._banner_hit_source_ok("ocr") is True
    assert kb._banner_hit_source_ok("ref") is True
    assert kb._banner_hit_source_ok("ref_owner") is True
    assert kb._banner_hit_source_ok("color") is False


def test_classify_frame_prefers_ref_before_ocr(monkeypatch) -> None:
    import numpy as np
    import mlbb_kill_banner as kb
    from unittest.mock import patch

    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    ref_hit = kb.KillBannerHit(sec=12.0, tier=3, label="triple", text="owner_pos", source="ref")

    monkeypatch.setenv("MLBB_BANNER_REF_BEFORE_OCR", "1")
    monkeypatch.setenv("MLBB_KILL_BANNER_COLOR_MIN", "0.01")

    with (
        patch.object(kb, "_announce_color_score", return_value=0.2),
        patch(
            "mlbb_banner_ref_match.classify_banner_reference",
            return_value=ref_hit,
        ),
        patch.object(kb, "_ocr_banner_zones") as ocr,
    ):
        out = kb._classify_frame(12.0, frame, deep=False, allow_ocr=True)
        ocr.assert_not_called()
    assert out is not None
    assert out.source == "ref"
    assert out.tier == 3
