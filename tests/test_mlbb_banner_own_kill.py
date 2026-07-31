#!/usr/bin/env python3
"""Own-kill banner: killer is LEFT; HUD portrait must match."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

cv2 = pytest.importorskip("cv2")

from mlbb_banner_hero_match import (  # noqa: E402
    extract_hud_hero_portrait_patch,
    extract_killer_portrait_patch,
    validate_own_kill_frame,
)

AUDIT = Path(__file__).resolve().parents[1] / "artifacts" / "banner_audit"


def _need_audit(*names: str) -> list[Path]:
    paths = [AUDIT / n for n in names]
    if not all(p.exists() for p in paths):
        pytest.skip("banner audit frames missing")
    return paths


def test_killer_portrait_is_left_of_banner_not_right() -> None:
    """Regression: old code took RIGHT (victim) as killer."""
    (path,) = _need_audit("TXr_565.5.jpg")
    frame = cv2.imread(str(path))
    assert frame is not None
    killer = extract_killer_portrait_patch(frame)
    assert killer is not None
    h, w = frame.shape[:2]
    # Old buggy right crop — should differ from fixed left crop.
    victim = frame[int(h * 0.07) : int(h * 0.20), int(w * 0.54) : int(w * 0.62)]
    victim = cv2.resize(victim, (48, 48))
    assert float(cv2.absdiff(killer, victim).mean()) > 5.0


def test_audit_rejects_ally_and_accepts_own_legendary() -> None:
    ally, own = _need_audit("TXr_485.1.jpg", "TXr_565.5.jpg")
    os.environ["MLBB_BANNER_OWN_KILL_REQUIRED"] = "1"
    os.environ["MLBB_BANNER_OWN_HUD_MIN_SIM"] = "0.22"
    ok_ally, reason_ally = validate_own_kill_frame(cv2.imread(str(ally)))
    ok_own, reason_own = validate_own_kill_frame(cv2.imread(str(own)))
    assert ok_ally is False, reason_ally
    assert "hud_killer_mismatch" in reason_ally or "unverifiable" in reason_ally
    assert ok_own is True, reason_own
    assert reason_own.startswith("hud_killer_ok")


def test_audit_rejects_rqu2_foreign_first_blood() -> None:
    (path,) = _need_audit("Rqu2_44.5.jpg")
    os.environ["MLBB_BANNER_OWN_KILL_REQUIRED"] = "1"
    os.environ["MLBB_BANNER_OWN_HUD_MIN_SIM"] = "0.22"
    ok, reason = validate_own_kill_frame(cv2.imread(str(path)))
    assert ok is False, reason


def test_hud_portrait_extractable_on_helcurt_vod() -> None:
    (path,) = _need_audit("TXr_565.5.jpg")
    hud = extract_hud_hero_portrait_patch(cv2.imread(str(path)))
    assert hud is not None
    assert hud.shape[:2] == (48, 48)
