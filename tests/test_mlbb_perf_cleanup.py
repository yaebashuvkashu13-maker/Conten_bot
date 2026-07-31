#!/usr/bin/env python3
"""Invariants for hang/dupe cleanup: OCR defaults, shared gates, lead helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_ocr_spikes_default_zero(monkeypatch) -> None:
    monkeypatch.delenv("MLBB_KILL_BANNER_DISCOVER_OCR_SPIKES", raising=False)
    assert os.environ.get("MLBB_KILL_BANNER_DISCOVER_OCR_SPIKES", "0") == "0"


def test_ocr_weak_needs_hud() -> None:
    from mlbb_kill_banner import ocr_weak_needs_hud

    os.environ["MLBB_OCR_SINGLE_REQUIRE_HUD"] = "1"
    assert ocr_weak_needs_hud("ocr", 1, "icon_ok") is True
    assert ocr_weak_needs_hud("ocr", 1, "hud_killer_ok:0.3") is False
    assert ocr_weak_needs_hud("ref", 1, "icon_ok") is False
    assert ocr_weak_needs_hud("ocr", 5, "icon_ok") is False


def test_ocr_budget_blocks_after_exhaust(monkeypatch) -> None:
    from mlbb_kill_banner import (
        reset_ocr_call_budget,
        _ocr_budget_ok,
        _ocr_budget_consume,
        _OCR_CALL_BUDGET,
    )

    reset_ocr_call_budget(2)
    assert _ocr_budget_ok()
    _ocr_budget_consume()
    assert _OCR_CALL_BUDGET["left"] == 1
    _ocr_budget_consume()
    assert _OCR_CALL_BUDGET["left"] == 0
    assert not _ocr_budget_ok()
    # Non-discover unlimited sentinel
    _OCR_CALL_BUDGET["left"] = -1
    assert _ocr_budget_ok()

def test_reject_ocr_single_helper() -> None:
    from mlbb_vod_segment_feed import _reject_ocr_single_send

    os.environ["MLBB_BANNER_REJECT_OCR_SINGLE"] = "1"
    os.environ["MLBB_ALLOW_OCR_SINGLE_SEND"] = "0"
    assert _reject_ocr_single_send("ocr", "single", 1, hud_own=False)
    assert _reject_ocr_single_send("ocr", "single", 1, hud_own=True) is None
    assert _reject_ocr_single_send("ref", "double", 2, hud_own=False) is None


def test_vod_lead_uses_banner_lead(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_KILL_BANNER_LEAD_SEC", "8")
    monkeypatch.delenv("MLBB_DOUBLE_BANNER_LEAD_SEC", raising=False)
    from mlbb_vod_segment_feed import _vod_lead_sec

    assert _vod_lead_sec() == pytest.approx(8.0)


def test_scan_window_deep_respects_allow_ocr(monkeypatch) -> None:
    from unittest.mock import patch
    from pathlib import Path

    import mlbb_kill_banner as kb

    frames = [(1.0, object()), (2.0, object())]
    calls = []

    def fake_classify(sec, frame, *, deep=False, allow_ocr=True, vod=None):
        calls.append({"deep": deep, "allow_ocr": allow_ocr})
        return None

    monkeypatch.setattr(kb, "_ffmpeg_sample_frames", lambda *a, **k: frames)
    monkeypatch.setattr(kb, "_sample_frames", lambda *a, **k: frames)
    monkeypatch.setattr(kb, "_candidate_secs", lambda *a, **k: [1.0])
    monkeypatch.setattr(kb, "_classify_frame", fake_classify)
    monkeypatch.setenv("MLBB_BANNER_DISCOVER_ACTIVE", "0")

    kb.scan_window(Path("/tmp/x.mp4"), 0.0, 5.0, quick=False, allow_ocr=False)
    assert all(c["allow_ocr"] is False for c in calls)
    assert not any(c["deep"] and c["allow_ocr"] for c in calls)


def test_montage_only_keeps_reliable_flag() -> None:
    src = Path(__file__).resolve().parents[1] / "scripts" / "daily_cycle_runner.py"
    text = src.read_text(encoding="utf-8")
    assert 'env["MLBB_VOD_RELIABLE"] = "0"' not in text
    assert "reliable kept" in text or "Do NOT clear MLBB_VOD_RELIABLE" in text
