"""Boss HP bar score must reject red environment wash (flight domains)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from gameplay_gate import _genshin_boss_bar_score  # noqa: E402


def _bgr(h: int, w: int, bgr: tuple[int, int, int]) -> np.ndarray:
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:, :] = bgr
    return frame


def test_red_environment_wash_scores_low() -> None:
    # Whole frame crimson — Natlan glide / domain fog false positive.
    frame = _bgr(720, 1280, (40, 40, 200))
    score = _genshin_boss_bar_score(frame)
    assert score < 0.15, score


def test_thin_top_bar_scores_high() -> None:
    frame = _bgr(720, 1280, (20, 20, 20))
    # Thin red/orange strip near top center (boss HP).
    y0, y1 = 18, 36
    x0, x1 = 280, 1000
    frame[y0:y1, x0:x1] = (30, 90, 220)
    score = _genshin_boss_bar_score(frame)
    assert score >= 0.28, score
