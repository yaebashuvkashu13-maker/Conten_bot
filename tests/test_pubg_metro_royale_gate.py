"""Tests for PUBG Metro Royale VOD/segment gate."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

if "cv2" not in sys.modules:
    cv2_stub = types.ModuleType("cv2")
    cv2_stub.resize = MagicMock(side_effect=lambda img, size: img)
    cv2_stub.cvtColor = MagicMock(side_effect=lambda img, code: img)
    cv2_stub.COLOR_BGR2GRAY = 0
    cv2_stub.COLOR_BGR2HSV = 0
    cv2_stub.threshold = MagicMock(return_value=(0, MagicMock()))
    cv2_stub.THRESH_BINARY = 0
    cv2_stub.THRESH_OTSU = 0
    sys.modules["cv2"] = cv2_stub

import numpy as np

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_metro_royale_gate import (  # noqa: E402
    segment_looks_metro_royale,
    title_metro_hint,
    vod_looks_metro_royale,
)


def test_title_metro_hint() -> None:
    assert title_metro_hint("Clutches in Metro Royale")
    assert title_metro_hint("96 Cash in метро роял")
    assert title_metro_hint("1v8 clutch PUBG(Metro Royal)")
    assert not title_metro_hint("Classic Mode Erangel ranked")
    assert not title_metro_hint("Training sniper - pubg metro royal")
    assert not title_metro_hint("Perfect no-recoil sensitivity Metro Royale")


def test_segment_trust_vod_skips_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_METRO_GATE", "1")
    monkeypatch.setenv("PUBG_METRO_SEGMENT_TRUST_VOD", "1")
    ok, reason = segment_looks_metro_royale(Path("x.mp4"), 100.0, 10.0)
    assert ok is True
    assert reason == "metro_vod_trusted"


def test_vod_title_trusted_skips_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_METRO_GATE", "1")
    monkeypatch.setenv("PUBG_METRO_TITLE_TRUST", "1")

    with patch("pubg_metro_royale_gate.segment_looks_metro_royale") as fake_segment:
        ok, reason = vod_looks_metro_royale(
            Path("x.mp4"),
            duration_sec=600.0,
            title="Best Metro Royale clutch",
        )
    fake_segment.assert_not_called()
    assert ok is True
    assert reason == "metro_title_trusted"


def test_vod_title_hint_needs_one_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_METRO_GATE", "1")
    monkeypatch.setenv("PUBG_METRO_TITLE_TRUST", "0")
    monkeypatch.setenv("PUBG_METRO_VOD_MIN_PROBES", "2")
    monkeypatch.setenv("PUBG_METRO_TITLE_HINT_MIN_PROBES", "1")

    def fake_segment(_path: Path, _start: float, _dur: float) -> tuple[bool, str]:
        if fake_segment.calls == 0:
            fake_segment.calls += 1
            return True, "metro_underground"
        fake_segment.calls += 1
        return False, "classic_outdoor_sky=2/3"

    fake_segment.calls = 0

    with patch("pubg_metro_royale_gate.segment_looks_metro_royale", side_effect=fake_segment):
        ok, reason = vod_looks_metro_royale(
            Path("x.mp4"),
            duration_sec=600.0,
            title="Best Metro Royale clutch",
        )
    assert ok is True
    assert "title_hint" in reason


def test_segment_relax_needs_three_outdoor_votes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_METRO_GATE", "1")
    monkeypatch.setenv("PUBG_METRO_MAX_SKY_RATIO", "0.15")

    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    sky_values = iter([0.20, 0.20, 0.05])

    def sky_ratio(_frame: np.ndarray) -> float:
        return next(sky_values, 0.05)

    with patch("pubg_metro_royale_gate._read_frame_at", return_value=frame), patch(
        "pubg_metro_royale_gate._frame_sky_ratio",
        side_effect=sky_ratio,
    ), patch(
        "pubg_metro_royale_gate._frame_mean_brightness",
        return_value=0.30,
    ), patch(
        "pubg_metro_royale_gate._ocr_metro_signals",
        return_value=(False, False),
    ):
        ok_strict, reason_strict = segment_looks_metro_royale(Path("x.mp4"), 100.0, 10.0)
        sky_values = iter([0.20, 0.20, 0.05])
        monkeypatch.setenv("PUBG_METRO_SEGMENT_RELAX", "1")
        ok_relax, reason_relax = segment_looks_metro_royale(Path("x.mp4"), 100.0, 10.0)

    assert ok_strict is False
    assert "classic_outdoor_sky=2/3" in reason_strict
    assert ok_relax is True
    assert reason_relax == "metro_underground"
