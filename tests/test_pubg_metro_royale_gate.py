"""Tests for PUBG Metro Royale visual gate."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pubg_metro_royale_gate import (  # noqa: E402
    METRO_MAP_RE,
    _frame_metro_minimap_tint,
    _ocr_metro_signals,
)


def test_metro_map_regex_matches_arctic():
    assert METRO_MAP_RE.search("arctic base evacuation")


def test_metro_map_regex_matches_russian():
    assert METRO_MAP_RE.search("эвакуация из туманного порта")


def test_minimap_tint_runs_on_frame():
    pytest.importorskip("cv2")
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    tint = _frame_metro_minimap_tint(frame)
    assert 0.0 <= tint <= 1.0


def test_ocr_metro_signals_classic_map(monkeypatch):
    pytest.importorskip("cv2")
    import pubg_metro_royale_gate as gate

    def fake_ocr(frame, *, y0, y1, x0, x1):
        return "Erangel classic ranked match"

    monkeypatch.setattr(gate, "_ocr_zone_text", fake_ocr)
    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    metro, classic, metro_map = _ocr_metro_signals(frame)
    assert classic is True
    assert metro is False
    assert metro_map is False
