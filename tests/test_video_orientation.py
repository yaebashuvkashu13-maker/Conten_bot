from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from video_orientation import (  # noqa: E402
    is_portrait_dimensions,
    is_portrait_frame,
    resize_for_analysis,
)


def test_is_portrait_dimensions() -> None:
    assert is_portrait_dimensions(1080, 1920)
    assert not is_portrait_dimensions(1920, 1080)


def test_resize_for_analysis_keeps_portrait_aspect() -> None:
    frame = np.zeros((1920, 1080, 3), dtype=np.uint8)
    small = resize_for_analysis(frame)
    assert small.shape[0] > small.shape[1]
    assert is_portrait_frame(small)
