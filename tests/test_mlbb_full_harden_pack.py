#!/usr/bin/env python3
"""One-pass harden pack: pool TTL, neighbor OCR budget, own-kill single, shooter CLIP scrub."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_pool_roundtrip_preserves_banner_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_scan_state import (
        invalidate_pool_cache,
        minimal_pool_from_entry,
        pool_cache_valid,
        pool_ttl_sec,
        record_vod_scan,
    )

    monkeypatch.setenv("VOD_POOL_TTL_SEC", "900")
    monkeypatch.setenv("MLBB_VOD_REUSE_PEAK_POOL", "1")
    assert pool_ttl_sec() == 900
    entry: dict = {}
    record_vod_scan(
        entry,
        sent=0,
        pool_peaks=[310.0],
        blocked=False,
        pool=[
            {
                "start": 310.0,
                "banner_sec": 312.0,
                "score": 0.9,
                "kill_banner_tier": 4,
                "kill_banner": "legendary",
                "banner_source": "ref",
                "banner_text": "LEGENDARY",
            }
        ],
    )
    assert entry["last_pool_peaks"][0]["banner_sec"] == 312.0
    assert entry["last_pool_peaks"][0]["banner_source"] == "ref"
    assert entry["last_pool_peaks"][0]["kill_banner_text"] == "LEGENDARY"
    pool = minimal_pool_from_entry(entry)
    assert pool[0]["peak_start"] == 312.0
    assert pool[0]["banner_sec"] == 312.0
    assert pool_cache_valid(entry) is True
    invalidate_pool_cache(entry, reason="stale")
    assert pool_cache_valid(entry) is False
    assert entry["last_pool_peaks"] == []


def test_presend_live_ocr_budget_caps_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    from mlbb_kill_banner import (
        _live_overlay_text,
        _presend_live_ocr_budget_ok,
        _presend_live_ocr_budget_reset,
        _PRESEND_LIVE_OCR_LEFT,
    )

    monkeypatch.setenv("MLBB_PRESEND_LIVE_OCR_BUDGET", "2")
    monkeypatch.setenv("MLBB_BANNER_LIVE_OVERLAY_OCR", "1")
    _presend_live_ocr_budget_reset()
    assert _PRESEND_LIVE_OCR_LEFT["left"] == 2

    with patch("mlbb_banner_ocr.read_banner_text", return_value=""), patch(
        "mlbb_kill_banner._ocr_banner_zones", return_value=""
    ):
        _live_overlay_text(object(), consume_presend_budget=True)
        _live_overlay_text(object(), consume_presend_budget=True)
        assert _presend_live_ocr_budget_ok() is False
        assert _live_overlay_text(object(), consume_presend_budget=True) == ""


def test_own_kill_single_passes_send_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    from mlbb_vod_segment_feed import _validate_before_send

    monkeypatch.setenv("MLBB_VOD_KILL_BANNER", "1")
    monkeypatch.setenv("MLBB_PRESEND_OWN_KILL_RECHECK", "1")
    monkeypatch.setenv("MLBB_PRESEND_OWN_KILL_SINGLE", "1")
    monkeypatch.setenv("MLBB_VOD_MONTAGE_SINGLE_FALLBACK", "1")
    monkeypatch.setenv("MLBB_BANNER_SEND_MIN_TIER", "double")
    monkeypatch.setenv("MLBB_VOD_BANNER_PRESEND", "0")
    monkeypatch.setenv("MLBB_PRESEND_REJECT_RUN", "0")
    monkeypatch.setenv("MLBB_PRESEND_REQUIRE_FIGHT_HUD", "0")
    monkeypatch.setenv("MLBB_BANNER_REJECT_OCR_SINGLE", "1")
    monkeypatch.setenv("MLBB_PRESEND_MAX_POST_RUN_FRAC", "1.0")
    monkeypatch.setenv("MLBB_PRESEND_BANNER_CONTEXT", "0")
    monkeypatch.setenv("MLBB_VOD_PRESEND_SKIP_VISUAL_ON_BANNER", "1")

    row = {
        "start": 100.0,
        "peak_start": 100.0,
        "banner_sec": 100.0,
        "kill_banner_tier": 1,
        "kill_banner": "single",
        "banner_source": "ref",
        "anchor": "kill_banner",
        "segment_id": "wb0_100",
    }
    rendered = Path("/tmp/fake_wb0.mp4")

    with patch(
        "mlbb_vod_segment_feed._detect_render_freeze", return_value=(True, "", [])
    ), patch(
        "mlbb_vod_segment_feed._segment_duration", return_value=2.0
    ), patch(
        "gameplay_gate._read_frame_at", return_value=object()
    ), patch(
        "mlbb_kill_banner._live_overlay_text", return_value=""
    ), patch(
        "mlbb_kill_banner._presend_live_ocr_budget_reset"
    ), patch(
        "mlbb_banner_hero_match.validate_own_kill_frame",
        return_value=(True, "hud_killer_ok:0.42"),
    ), patch(
        "mlbb_vod_montage.clip_run_fraction", return_value=0.0
    ), patch(
        "mlbb_vod_segment_feed._vod_crop_box", return_value=None
    ), patch(
        "gameplay_gate.score_segment_combat", return_value=(0.05, 0.02, 0.02, "")
    ), patch(
        "gameplay_gate.segment_looks_like_draft_or_queue", return_value=False
    ), patch(
        "gameplay_gate.segment_uniform_gameplay_ok", return_value=(True, "ok")
    ), patch(
        "visual_action_check.extract_and_check_segment",
        return_value={"visual_pass": True, "fail_reason": ""},
    ), patch(
        "mlbb_vod_segment_feed._presend_min_motion", return_value=0.0
    ), patch(
        "mlbb_vod_segment_feed._presend_min_minimap_delta", return_value=0.0
    ):
        ok, reason, report = _validate_before_send(Path("/tmp/vod.mp4"), row, rendered)
    assert ok is True, reason
    assert report.get("own_kill_single_send") is True
    assert "kill_banner_tier_low" not in reason


def test_scrub_clip_disabled_for_shooter(monkeypatch: pytest.MonkeyPatch) -> None:
    import daily_cycle_runner as dcr

    monkeypatch.setenv("HIGHLIGHT_CLIP_DISABLED", "1")
    env = {"HIGHLIGHT_CLIP_DISABLED": "1", "MLBB_BANNER_SKIP_CLIP_SCORE": "1"}
    dcr._scrub_mlbb_only_env_for_shooter(env)
    assert env["HIGHLIGHT_CLIP_DISABLED"] == "0"
    assert "MLBB_BANNER_SKIP_CLIP_SCORE" not in env


def test_fight_first_soften_clamps_quick_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from mlbb_vod_adaptive_gate import overrides_for_level

    monkeypatch.setenv("MLBB_BANNER_FIGHT_FIRST", "1")
    ov = overrides_for_level(2)
    assert int(ov["MLBB_KILL_BANNER_QUICK_BEFORE"]) <= 4
    assert int(ov["MLBB_KILL_BANNER_QUICK_AFTER"]) <= 8


def test_kill_rich_spike_defaults_capped() -> None:
    assert float(os.environ.get("MLBB_FIGHT_FIRST_KILL_RICH_SPIKE_SEC", "45")) <= 45
    assert int(os.environ.get("MLBB_FIGHT_FIRST_KILL_RICH_SPIKE_PROBES", "8")) <= 8


def test_reliable_keep_banner_miss_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from mlbb_vod_segment_feed import _apply_mlbb_reliable_runtime

    monkeypatch.delenv("MLBB_VOD_KEEP_BANNER_MISS", raising=False)
    monkeypatch.setenv("MLBB_VOD_RELIABLE", "1")
    _apply_mlbb_reliable_runtime()
    assert os.environ["MLBB_VOD_KEEP_BANNER_MISS"] == "1"
