"""PUBG fight-length shipping: long fights, single fallback."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from shooter_vod_segment_feed import _pubg_duration_cap  # noqa: E402


def test_long_fight_montage_part_cap() -> None:
    assert _pubg_duration_cap(38.0, single=False) == 38.0
    assert _pubg_duration_cap(15.0, single=False) <= 28.0


def test_single_fight_allows_longer() -> None:
    assert _pubg_duration_cap(72.0, single=True) == 72.0


def test_shape_gate_rejects_running_padding() -> None:
    from pubg_clip_shape_gate import validate_clip_fight_shape

    ok, reason = validate_clip_fight_shape(
        0.0,
        40.0,
        38.0,
        {"shooting_start": 35.0, "fight_end": 42.0},
    )
    assert not ok
    assert "prefight" in reason
