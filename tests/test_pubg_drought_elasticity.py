"""Tests for PUBG drought elasticity (−15%/idle-hour, +10% after send)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pubg_drought_elasticity as el  # noqa: E402


@pytest.fixture(autouse=True)
def _iso_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import pubg_drought_elasticity as el
    monkeypatch.setenv("PUBG_DROUGHT_ELASTICITY", "1")
    monkeypatch.setenv("PUBG_DROUGHT_ELASTICITY_PATH", str(tmp_path / "elasticity.json"))
    monkeypatch.setenv("PUBG_DROUGHT_ELASTICITY_BOOT_IDLE_HOURS", "0")
    saved = {k: __import__("os").environ.get(k) for k in (*el.ELASTIC_KEYS, "PUBG_DROUGHT_ELASTICITY_ACTIVE", "PUBG_REJECT_BOT_FARM", "PUBG_HARD_REJECT_MENU_OVERLAY")}
    yield
    import os
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_scale_hardens_right_after_send() -> None:
    assert el.elasticity_scale(hours_idle=0.0) == pytest.approx(1.10)
    assert el.elasticity_scale(hours_idle=0.5) == pytest.approx(1.10)


def test_scale_softens_15pct_per_idle_hour() -> None:
    assert el.elasticity_scale(hours_idle=1.0) == pytest.approx(0.85)
    assert el.elasticity_scale(hours_idle=2.0) == pytest.approx(0.70)
    assert el.elasticity_scale(hours_idle=5.0) == pytest.approx(0.70)


def test_apply_scales_numeric_keeps_hard_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_REJECT_BOT_FARM", "0")  # should be forced back on
    monkeypatch.setenv("PUBG_HARD_REJECT_MENU_OVERLAY", "0")
    info = el.apply_elasticity_to_environ(hours_idle=2.0)
    assert info["enabled"] is True
    assert info["scale"] == pytest.approx(0.70)
    base = float(el.DEFAULT_BASELINE["PUBG_PAYOFF_SCORE_MIN_SINGLES"])
    assert float(__import__("os").environ["PUBG_PAYOFF_SCORE_MIN_SINGLES"]) == pytest.approx(
        base * 0.70, rel=1e-3
    )
    assert __import__("os").environ["PUBG_REJECT_BOT_FARM"] == "1"
    assert __import__("os").environ["PUBG_HARD_REJECT_MENU_OVERLAY"] == "1"
    assert __import__("os").environ["PUBG_DROUGHT_ELASTICITY_ACTIVE"] == "1"


def test_note_send_updates_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "elasticity.json"
    monkeypatch.setenv("PUBG_DROUGHT_ELASTICITY_PATH", str(path))
    el.note_successful_send(ts=1_700_000_000.0)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["last_sent_ts"] == pytest.approx(1_700_000_000.0)
