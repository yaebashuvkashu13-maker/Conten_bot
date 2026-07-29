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


def test_send_min_tier_floors_double(monkeypatch) -> None:
    from mlbb_kill_banner import send_min_tier

    monkeypatch.setenv("MLBB_KILL_BANNER_MIN_TIER", "single")
    monkeypatch.delenv("MLBB_BANNER_SEND_MIN_TIER", raising=False)
    monkeypatch.setenv("MLBB_ADAPTIVE_ALLOW_SINGLE", "0")
    assert send_min_tier() == 2
    monkeypatch.setenv("MLBB_ADAPTIVE_ALLOW_SINGLE", "1")
    assert send_min_tier() == 1
    monkeypatch.setenv("MLBB_BANNER_SEND_MIN_TIER", "triple")
    assert send_min_tier() == 3


def test_may_trust_discover_rejects_ocr_single(monkeypatch) -> None:
    from mlbb_kill_banner import _may_trust_discover_banner

    monkeypatch.setenv("MLBB_VOD_BANNER_PRESEND_TRUST_DISCOVER", "1")
    assert (
        _may_trust_discover_banner(
            {"kill_banner": "single", "kill_banner_tier": 1, "banner_source": "ocr"}
        )
        is False
    )
    assert (
        _may_trust_discover_banner(
            {"kill_banner": "double", "kill_banner_tier": 2, "banner_source": "ref:owner"}
        )
        is True
    )
    monkeypatch.setenv("MLBB_VOD_BANNER_PRESEND_TRUST_DISCOVER", "0")
    monkeypatch.setenv("MLBB_VOD_PRESEND_TRUST_DISCOVERY", "0")
    assert (
        _may_trust_discover_banner(
            {"kill_banner": "double", "kill_banner_tier": 2, "banner_source": "ref"}
        )
        is False
    )


def test_accept_ocr_rejects_garbled_kill(monkeypatch) -> None:
    """Bare OCR 'kill' in HUD noise must not become a shippable single."""
    import numpy as np
    from unittest.mock import patch

    from mlbb_kill_banner import _classify_frame, KillBannerHit

    monkeypatch.setenv("MLBB_BANNER_OCR_WEAK_SINGLE", "0")
    monkeypatch.setenv("MLBB_BANNER_REF_BEFORE_OCR", "0")
    monkeypatch.setenv("MLBB_KILL_BANNER_COLOR_ONLY", "0")
    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    garbled = KillBannerHit(
        sec=0.0,
        tier=1,
        label="single_weak",
        text="_ J na | be a? * i eee WY, L,. c kill",
        source="ocr",
    )
    with patch("mlbb_kill_banner._announce_color_score", return_value=0.2):
        with patch("mlbb_kill_banner._ocr_banner_zones", return_value=garbled.text):
            with patch("mlbb_kill_banner.classify_banner_text", return_value=garbled):
                assert _classify_frame(10.0, frame, deep=True) is None

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


def test_bounds_hard_cut_after_banner_not_fight_end(monkeypatch) -> None:
    """Post-fight sustain must not keep lane-jog — end = banner + 3s."""
    monkeypatch.setenv("MLBB_VOD_LEAD_SEC", "12")
    monkeypatch.setenv("MLBB_KILL_BANNER_LEAD_SEC", "12")
    monkeypatch.delenv("MLBB_BANNER_PRE_SEC", raising=False)
    monkeypatch.setenv("MLBB_FIGHT_MIN_SEC", "8")
    monkeypatch.setenv("MLBB_FIGHT_MAX_SEC", "40")
    monkeypatch.setenv("MLBB_FIGHT_HARD_MAX_SEC", "65")
    monkeypatch.setenv("MLBB_BANNER_POST_SEC", "3")
    monkeypatch.setenv("MLBB_BANNER_IDEAL_MIN", "0")
    start, end, dur = bounds_from_banner(
        100.0,
        file_dur=200.0,
        fight_start=88.0,
        fight_end=140.0,  # motion sustain includes long run
        banner_tier=2,
    )
    assert end == 103.0
    assert start <= 88.0
    assert dur == end - start


def test_bounds_from_fight_sustain() -> None:
    os.environ["MLBB_VOD_LEAD_SEC"] = "12"
    os.environ["MLBB_KILL_BANNER_LEAD_SEC"] = "12"
    os.environ.pop("MLBB_BANNER_PRE_SEC", None)
    os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
    os.environ["MLBB_FIGHT_MAX_SEC"] = "40"
    os.environ["MLBB_FIGHT_HARD_MAX_SEC"] = "65"
    os.environ["MLBB_BANNER_POST_SEC"] = "3"
    os.environ["MLBB_BANNER_IDEAL_MIN"] = "0"
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
    assert end == 103.0  # banner + 3s, not fight_end
    assert end >= 100.0
    assert dur >= 8.0


def test_bounds_fallback_without_fight() -> None:
    os.environ["MLBB_VOD_LEAD_SEC"] = "12"
    os.environ["MLBB_KILL_BANNER_LEAD_SEC"] = "12"
    os.environ.pop("MLBB_BANNER_PRE_SEC", None)
    os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
    os.environ["MLBB_FIGHT_MAX_SEC"] = "40"
    os.environ["MLBB_BANNER_POST_SEC"] = "3"
    os.environ["MLBB_BANNER_IDEAL_MIN"] = "0"
    start, end, dur = bounds_from_banner(50.0, file_dur=120.0, banner_tier=2)
    assert start == 38.0
    assert end == 53.0
    assert 8.0 <= dur <= 40.0


def test_bounds_never_puts_banner_at_2_3_seconds() -> None:
    os.environ["MLBB_KILL_BANNER_LEAD_SEC"] = "14"
    os.environ["MLBB_VOD_LEAD_SEC"] = "4"  # legacy short — must not win for banners
    os.environ["MLBB_BANNER_PRE_SEC"] = "2"  # regressive short — must not shrink below lead
    os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
    os.environ["MLBB_FIGHT_MAX_SEC"] = "55"
    os.environ["MLBB_FIGHT_HARD_MAX_SEC"] = "65"
    os.environ["MLBB_BANNER_POST_SEC"] = "3"
    os.environ["MLBB_BANNER_IDEAL_MIN"] = "0"
    start, end, dur = bounds_from_banner(
        100.0,
        file_dur=200.0,
        fight_start=98.0,
        fight_end=120.0,
        banner_tier=3,
    )
    banner_at = 100.0 - start
    assert banner_at >= 12.0, f"banner too early at {banner_at:.1f}s"
    assert end == 103.0
    assert dur >= 8.0


def test_discover_hit_target_honors_send_all() -> None:
    import mlbb_kill_banner as kb

    old = {
        k: os.environ.get(k)
        for k in (
            "MLBB_VOD_SEND_ALL_BANNERS",
            "MLBB_KILL_BANNER_DISCOVER_MIN_HITS",
            "MLBB_KILL_BANNER_DISCOVER_TARGET",
            "MLBB_VOD_MAX_PER_VOD",
            "MLBB_VOD_MONTAGE",
            "MLBB_SKIP_MONTAGE",
            "MLBB_VOD_MONTAGE_MIN_CLIPS",
        )
    }
    try:
        os.environ["MLBB_VOD_MONTAGE"] = "0"
        os.environ["MLBB_SKIP_MONTAGE"] = "0"
        os.environ["MLBB_VOD_SEND_ALL_BANNERS"] = "1"
        os.environ["MLBB_KILL_BANNER_DISCOVER_MIN_HITS"] = "2"
        os.environ.pop("MLBB_KILL_BANNER_DISCOVER_TARGET", None)
        os.environ["MLBB_VOD_MAX_PER_VOD"] = "5"
        assert kb._discover_hit_target() == 5

        os.environ["MLBB_VOD_SEND_ALL_BANNERS"] = "0"
        assert kb._discover_hit_target() == 2

        os.environ["MLBB_VOD_MONTAGE"] = "1"
        os.environ["MLBB_VOD_MONTAGE_MIN_CLIPS"] = "3"
        assert kb._discover_hit_target() == 3

        os.environ["MLBB_VOD_SEND_ALL_BANNERS"] = "1"
        os.environ["MLBB_VOD_MONTAGE"] = "0"
        os.environ["MLBB_KILL_BANNER_DISCOVER_TARGET"] = "7"
        assert kb._discover_hit_target() == 7
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_discover_peak_budget_reaches_spike(monkeypatch) -> None:
    """Peak OCR must not consume the whole wall budget before spike sweep."""
    from unittest.mock import patch

    import mlbb_kill_banner as kb

    vod = Path("/tmp/fake_budget_vod.mp4")
    fake_analysis = {
        "duration": 900.0,
        "window_seconds": 2.0,
        "center_motion": [0.01] * 50 + [0.9] * 20 + [0.02] * 380,
        "audio": [0.01] * 450,
    }
    calls = {"peak": 0, "spike": 0, "t": 1000.0}

    def fake_time():
        return calls["t"]

    def fake_find(vod_path, peak, *, quick=False, allow_ocr=True):
        calls["peak"] += 1
        # Each peak OCR used to burn ~25s — simulate cost but stay under peak budget.
        calls["t"] += 8.0 if allow_ocr else 1.0
        return None

    def fake_scan(*_a, **kw):
        calls["spike"] += 1
        calls["t"] += 2.0
        # First spike OCR hit after peaks.
        if calls["spike"] >= 3 and kw.get("allow_ocr", True):
            return [kb.KillBannerHit(sec=400.0, tier=2, label="double", text="DOUBLE", source="ocr")]
        return []

    monkeypatch.setenv("MLBB_VOD_BANNER_DISCOVER", "1")
    monkeypatch.setenv("MLBB_VOD_KILL_BANNER", "1")
    monkeypatch.setenv("MLBB_VOD_BANNER_DENSE_SEC", "0")
    monkeypatch.setenv("MLBB_VOD_SEND_ALL_BANNERS", "0")
    monkeypatch.setenv("MLBB_VOD_MONTAGE", "0")
    monkeypatch.setenv("MLBB_KILL_BANNER_DISCOVER_MIN_HITS", "1")
    monkeypatch.setenv("MLBB_KILL_BANNER_DISCOVER_MAX_PROBES", "40")
    monkeypatch.setenv("MLBB_KILL_BANNER_DISCOVER_MAX_SEC", "100")
    monkeypatch.setenv("MLBB_KILL_BANNER_DISCOVER_PEAK_BUDGET_FRAC", "0.40")
    monkeypatch.setenv("MLBB_KILL_BANNER_DISCOVER_PEAK_HINTS", "8")
    monkeypatch.setenv("MLBB_KILL_BANNER_DISCOVER_PEAK_FULL_RETRY", "2")
    monkeypatch.setenv("MLBB_KILL_BANNER_DISCOVER_SPIKE_CAP", "20")
    monkeypatch.setenv("MLBB_VOD_BANNER_DISCOVER_SPIKE", "1")
    monkeypatch.setenv("MLBB_BANNER_REF_MATCH", "1")
    monkeypatch.setenv("MLBB_VOD_MIN_PEAK_SEC", "0")

    with (
        patch("mlbb_fight_segment._analysis_for", return_value=fake_analysis),
        patch.object(kb, "find_banner_near_peak", side_effect=fake_find),
        patch.object(kb, "scan_window", side_effect=fake_scan),
        patch.object(kb.time, "monotonic", side_effect=fake_time),
        patch(
            "mlbb_owner_learning.owner_kill_anchor_secs_for_path",
            return_value=[],
        ),
    ):
        hits = kb.discover_vod_kill_banners(
            vod, hint_peaks=[100.0, 140.0, 180.0, 220.0, 260.0, 300.0, 340.0, 380.0]
        )
    assert calls["spike"] >= 1, "spike sweep must run after peak budget"
    assert hits and hits[0].tier >= 2


def test_discover_auto_dense_for_maniac_tier(monkeypatch) -> None:
    import sys
    import types
    from unittest.mock import patch

    import mlbb_kill_banner as kb

    vod = Path("/tmp/fake_dense_vod.mp4")
    fake_analysis = {
        "duration": 400.0,
        "window_seconds": 2.0,
        "center_motion": [0.1] * 200,
        "audio": [0.1] * 200,
    }
    seen = {"dense": False}

    # Dense path imports gameplay_gate/cv2 — stub if missing in test env.
    if "cv2" not in sys.modules:
        cv2_stub = types.ModuleType("cv2")

        class _Cap:
            def isOpened(self):
                return False

            def release(self):
                return None

        cv2_stub.VideoCapture = lambda *_a, **_k: _Cap()
        sys.modules["cv2"] = cv2_stub
    if "gameplay_gate" not in sys.modules:
        gg = types.ModuleType("gameplay_gate")
        gg._read_frame_at = lambda *_a, **_k: None
        sys.modules["gameplay_gate"] = gg

    monkeypatch.setenv("MLBB_VOD_BANNER_DISCOVER", "1")
    monkeypatch.setenv("MLBB_VOD_KILL_BANNER", "1")
    monkeypatch.setenv("MLBB_VOD_BANNER_DENSE_SEC", "0")
    monkeypatch.setenv("MLBB_VOD_TITLE_DENSE_AUTO", "1")
    monkeypatch.setenv("MLBB_KILL_BANNER_DISCOVER_MAX_PROBES", "20")
    monkeypatch.setenv("MLBB_KILL_BANNER_DISCOVER_MAX_SEC", "30")
    monkeypatch.setenv("MLBB_KILL_BANNER_DISCOVER_STEP", "30")
    monkeypatch.setenv("MLBB_VOD_MIN_PEAK_SEC", "0")
    monkeypatch.setenv("MLBB_VOD_TITLE_MIN_TIER", "4")

    real_log = kb.log.info

    def spy_info(msg, *args, **kwargs):
        text = msg % args if args else str(msg)
        if "dense_1hz" in text or "auto-dense" in text:
            seen["dense"] = True
        return real_log(msg, *args, **kwargs)

    with (
        patch("mlbb_fight_segment._analysis_for", return_value=fake_analysis),
        patch.object(kb.log, "info", side_effect=spy_info),
        patch(
            "mlbb_owner_learning.owner_kill_anchor_secs_for_path",
            return_value=[],
        ),
    ):
        kb.discover_vod_kill_banners(vod, min_tier=4, hint_peaks=[60.0])
    assert seen["dense"] is True


def test_discover_keeps_sweeping_until_target() -> None:
    """Spike pass must not stop at MIN_HITS=2 when SEND_ALL targets 5."""
    import numpy as np
    import mlbb_kill_banner as kb
    from unittest.mock import patch

    vod = Path("/tmp/fake_vod_target.mp4")
    fake_analysis = {
        "duration": 600.0,
        "window_seconds": 2.0,
        "center_motion": np.linspace(0.01, 0.9, 300, dtype=np.float32),
        "audio": np.linspace(0.0, 0.8, 300, dtype=np.float32),
    }
    calls = {"n": 0}

    def fake_probe_hits(*_a, **_kw):
        calls["n"] += 1
        # Emit a unique hit every call so merge keeps growing.
        sec = 100.0 + calls["n"] * 20.0
        return [
            kb.KillBannerHit(sec=sec, tier=2, label="double", text="DOUBLE", source="ref")
        ]

    old = {
        k: os.environ.get(k)
        for k in (
            "MLBB_VOD_BANNER_DISCOVER",
            "MLBB_VOD_KILL_BANNER",
            "MLBB_VOD_SEND_ALL_BANNERS",
            "MLBB_KILL_BANNER_DISCOVER_MIN_HITS",
            "MLBB_KILL_BANNER_DISCOVER_TARGET",
            "MLBB_KILL_BANNER_DISCOVER_MAX_PROBES",
            "MLBB_KILL_BANNER_DISCOVER_MAX_SEC",
            "MLBB_KILL_BANNER_DISCOVER_SPIKE_CAP",
            "MLBB_VOD_BANNER_DISCOVER_SPIKE",
            "MLBB_BANNER_REF_MATCH",
        )
    }
    os.environ.update(
        {
            "MLBB_VOD_BANNER_DISCOVER": "1",
            "MLBB_VOD_KILL_BANNER": "1",
            "MLBB_VOD_SEND_ALL_BANNERS": "1",
            "MLBB_KILL_BANNER_DISCOVER_MIN_HITS": "2",
            "MLBB_KILL_BANNER_DISCOVER_TARGET": "5",
            "MLBB_KILL_BANNER_DISCOVER_MAX_PROBES": "40",
            "MLBB_KILL_BANNER_DISCOVER_MAX_SEC": "60",
            "MLBB_KILL_BANNER_DISCOVER_SPIKE_CAP": "30",
            "MLBB_VOD_BANNER_DISCOVER_SPIKE": "1",
            "MLBB_BANNER_REF_MATCH": "1",
        }
    )
    try:
        with (
            patch("mlbb_fight_segment._analysis_for", return_value=fake_analysis),
            patch.object(kb, "find_banner_near_peak", return_value=None),
            patch.object(kb, "scan_window", side_effect=fake_probe_hits),
            patch(
                "mlbb_owner_learning.owner_kill_anchor_secs_for_path",
                return_value=[],
            ),
        ):
            hits = kb.discover_vod_kill_banners(vod, hint_peaks=[120.0, 200.0])
        assert len(hits) >= 5
        assert calls["n"] >= 5
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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
    os.environ["MLBB_BANNER_POST_SEC"] = "3"
    os.environ["MLBB_BANNER_IDEAL_MIN"] = "0"
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
    assert end == 30.0  # banner + 3s
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
