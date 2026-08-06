"""Author-kill / author-death gate for shooter montages."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import shooter_author_kill_gate as gate  # noqa: E402


def _fake_frame(mean: float = 90.0, noise: float = 40.0) -> np.ndarray:
    rng = np.random.default_rng(0)
    base = np.full((180, 320, 3), float(mean), dtype=np.float32)
    base += rng.normal(0.0, noise, base.shape)
    return np.clip(base, 0, 255).astype(np.uint8)


def test_death_ocr_rejects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_AUTHOR_KILL_GATE", "1")
    monkeypatch.setenv("SHOOTER_REJECT_AUTHOR_DEATH", "1")
    monkeypatch.setenv("SHOOTER_REQUIRE_AUTHOR_KILL", "0")
    vod = tmp_path / "x.mp4"
    vod.write_bytes(b"x")

    ocr_cycle = ["", "", "", "", "You were killed by Enemy", "", "", ""]

    with patch.object(gate, "_read_frame", return_value=_fake_frame()), patch.object(
        gate, "_ocr_blob", side_effect=ocr_cycle
    ), patch("gameplay_gate.detect_game_viewport_crop", return_value=None):
        ok, reason, _m = gate.author_kill_window_ok(vod, 10.0, 12.0, profile="standoff")
    assert ok is False
    assert "author_death" in reason


def test_kill_then_death_allowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_AUTHOR_KILL_GATE", "1")
    monkeypatch.setenv("SHOOTER_REJECT_AUTHOR_DEATH", "1")
    monkeypatch.setenv("SHOOTER_REQUIRE_AUTHOR_KILL", "1")
    vod = tmp_path / "x.mp4"
    vod.write_bytes(b"x")

    with patch.object(
        gate,
        "detect_author_kill_signals",
        return_value=(True, "author_kill_hitflash=0.01", {"hit_flash": 0.01}),
    ), patch.object(
        gate,
        "detect_author_death_signals",
        return_value=(True, "author_death_ocr=killed by", {}),
    ):
        ok, reason, m = gate.author_kill_window_ok(vod, 10.0, 12.0, profile="standoff")
    assert ok is True
    assert m.get("death_after_kill") is True
    assert "author_kill" in reason


def test_require_author_kill_rejects_gunfight_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SHOOTER_AUTHOR_KILL_GATE", "1")
    monkeypatch.setenv("SHOOTER_REJECT_AUTHOR_DEATH", "1")
    monkeypatch.setenv("SHOOTER_REQUIRE_AUTHOR_KILL", "1")
    vod = tmp_path / "x.mp4"
    vod.write_bytes(b"x")

    with patch.object(
        gate,
        "detect_author_kill_signals",
        return_value=(False, "no_author_kill", {"hit_flash": 0.0}),
    ), patch.object(
        gate,
        "detect_author_death_signals",
        return_value=(False, "", {}),
    ):
        ok, reason, _m = gate.author_kill_window_ok(vod, 10.0, 12.0, profile="standoff")
    assert ok is False
    assert reason == "no_author_kill"
