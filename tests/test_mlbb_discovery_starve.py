#!/usr/bin/env python3
from __future__ import annotations

import sys
import types
from pathlib import Path

# Feed imports gameplay_gate → cv2; stub for unit tests without OpenCV.
if "cv2" not in sys.modules:
    cv2 = types.ModuleType("cv2")
    cv2.VideoCapture = lambda *_a, **_k: None
    sys.modules["cv2"] = cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_vod_segment_feed import (  # noqa: E402
    _title_promise_revive_ok,
    _zero_yield_block_active,
)


def test_title_promise_revive_ok() -> None:
    assert _title_promise_revive_ok("26 Kills + MANIAC!! MVP Dyrroth")
    assert _title_promise_revive_ok("SAVAGE teamfight ranked")
    assert _title_promise_revive_ok("DOUBLE KILL clutch")
    assert not _title_promise_revive_ok("Paquito build guide mythical")


def test_zero_yield_block_bypasses_when_starving(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_VOD_BYPASS_ZERO_YIELD", "0")
    monkeypatch.setenv("MLBB_VOD_ZERO_YIELD_BYPASS_AFTER", "3")
    monkeypatch.setattr(
        "mlbb_vod_segment_feed._discovery_starvation_level",
        lambda: 5,
    )
    assert _zero_yield_block_active() is False
    monkeypatch.setattr(
        "mlbb_vod_segment_feed._discovery_starvation_level",
        lambda: 1,
    )
    assert _zero_yield_block_active() is True
    monkeypatch.setenv("MLBB_VOD_BYPASS_ZERO_YIELD", "1")
    assert _zero_yield_block_active() is False
