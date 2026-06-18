"""Tests for MLBB-only Shorts title gate."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

cv2 = pytest.importorskip("cv2")  # noqa: F401

from gameplay_gate import OTHER_GAME_TITLE, is_mlbb_calibration_short  # noqa: E402


def test_other_game_title_rejected() -> None:
    assert OTHER_GAME_TITLE.search("PUBG insane clutch shorts")
    assert OTHER_GAME_TITLE.search("Standoff 2 montage")
    assert not OTHER_GAME_TITLE.search("MLBB savage teamfight shorts")


def test_promo_title_rejected(tmp_path: Path) -> None:
    # No real video — title check only needs path name + description.
    fake = tmp_path / "yt_test.mp4"
    fake.write_bytes(b"x" * 1000)
    ok, _score, reason = is_mlbb_calibration_short(
        fake, description="PUBG mobile highlights #shorts"
    )
    assert not ok
    assert reason == "other_game_title"
