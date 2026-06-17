"""Tests for minimap HSV dot counting."""

from __future__ import annotations

import numpy as np
import cv2

from mlbb_minimap_analyze import _count_team_dots, _extract_minimap


def test_count_dots_on_synthetic_minimap() -> None:
    patch = np.zeros((90, 90, 3), dtype=np.uint8)
    # blue dot
    cv2.circle(patch, (20, 20), 6, (255, 120, 60), -1)
    # red dot
    cv2.circle(patch, (60, 50), 6, (60, 60, 255), -1)
    ally, enemy = _count_team_dots(patch)
    assert ally >= 1 or enemy >= 1


def test_extract_minimap_returns_patch() -> None:
    frame = np.zeros((1920, 1080, 3), dtype=np.uint8)
    patch = _extract_minimap(frame, (0.0, 0.72, 0.28, 1.0))
    assert patch.size > 0
