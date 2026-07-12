"""Tests for kill-banner vs POV hero portrait matching."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_banner_pov_match import (  # noqa: E402
    banner_pov_hero_match,
    extract_banner_hero_patch,
    extract_pov_hero_patch,
    portrait_similarity,
)


def test_portrait_similarity_identical() -> None:
    patch = np.zeros((48, 48, 3), dtype=np.uint8)
    patch[:, :, 1] = 120
    patch[10:38, 10:38, 0] = 40
    sim = portrait_similarity(patch, patch.copy())
    assert sim >= 0.99


def test_portrait_similarity_different() -> None:
    a = np.zeros((48, 48, 3), dtype=np.uint8)
    a[:, :, 2] = 200
    b = np.zeros((48, 48, 3), dtype=np.uint8)
    b[:, :, 0] = 200
    sim = portrait_similarity(a, b)
    assert sim < 0.5


def test_extract_patches_from_frame() -> None:
    pytest.importorskip("cv2")
    import cv2

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[20:140, 80:260] = (30, 180, 220)
    frame[520:680, 20:160] = (30, 180, 220)
    banner = extract_banner_hero_patch(frame)
    pov = extract_pov_hero_patch(frame)
    assert banner is not None and pov is not None
    assert banner.shape == (48, 48, 3)
    assert portrait_similarity(banner, pov) >= 0.85


def test_banner_pov_match_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MLBB_BANNER_POV_MATCH", "0")
    ok, reason, sim = banner_pov_hero_match(Path("/nonexistent.mp4"), 10.0)
    assert ok is True
    assert reason == "pov_match_off"
    assert sim == 1.0


def test_banner_pov_for_peak_tries_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    from mlbb_banner_pov_match import banner_pov_hero_match_for_peak

    monkeypatch.setenv("MLBB_BANNER_POV_RESCAN", "1")
    calls: list[float] = []

    def fake_match(vod: Path, sec: float) -> tuple[bool, str, float]:
        calls.append(sec)
        if abs(sec - 79.3) < 0.1:
            return True, "pov_hero_ok sim=0.58", 0.58
        return False, "pov_hero_mismatch", 0.06

    monkeypatch.setattr("mlbb_banner_pov_match.banner_pov_hero_match", fake_match)
    monkeypatch.setattr(
        "mlbb_kill_banner.find_banner_near_peak",
        lambda vod, peak, quick=True: type("Hit", (), {"sec": 79.3})(),
    )

    ok, reason, sim = banner_pov_hero_match_for_peak(Path("x.mp4"), 84.0, banner_sec=84.0)
    assert ok is True
    assert 79.3 in calls or 80.0 in calls


def test_explicit_banner_time_skips_redundant_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    from mlbb_banner_pov_match import banner_pov_hero_match_for_peak

    monkeypatch.setenv("MLBB_BANNER_POV_RESCAN", "0")
    monkeypatch.setattr(
        "mlbb_banner_pov_match.banner_pov_hero_match",
        lambda _vod, sec: (True, "pov_hero_ok", 0.8)
        if abs(sec - 84.0) < 0.1
        else (False, "mismatch", 0.0),
    )

    def unexpected_rescan(*_args, **_kwargs):
        raise AssertionError("OCR rescan should not run for explicit banner_sec")

    monkeypatch.setattr("mlbb_kill_banner.find_banner_near_peak", unexpected_rescan)
    ok, reason, sim = banner_pov_hero_match_for_peak(
        Path("x.mp4"),
        84.0,
        banner_sec=84.0,
    )
    assert ok is True
    assert reason == "pov_hero_ok"
    assert sim == 0.8
