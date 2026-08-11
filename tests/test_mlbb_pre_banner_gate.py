"""Kill-banner clips must include fight before the banner, not post-fight tail."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_lead_sec_uses_kill_banner_min_pre() -> None:
    from mlbb_fight_segment import _lead_sec

    os.environ["MLBB_KILL_BANNER_LEAD_SEC"] = "8"
    os.environ["MLBB_VOD_LEAD_SEC"] = "4"
    os.environ["MLBB_KILL_BANNER_MIN_PRE_SEC"] = "5"
    try:
        assert _lead_sec() == 8.0
    finally:
        for k in ("MLBB_KILL_BANNER_LEAD_SEC", "MLBB_VOD_LEAD_SEC", "MLBB_KILL_BANNER_MIN_PRE_SEC"):
            os.environ.pop(k, None)


def test_bounds_enforces_min_pre_banner() -> None:
    from mlbb_kill_banner import bounds_from_banner

    os.environ["MLBB_KILL_BANNER_MIN_PRE_SEC"] = "5"
    os.environ["MLBB_KILL_BANNER_POST_SEC"] = "3"
    os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
    os.environ["MLBB_FIGHT_MAX_SEC"] = "28"
    try:
        start, end, dur = bounds_from_banner(
            76.8,
            file_dur=200.0,
            fight_start=74.0,
            fight_end=82.0,
        )
        assert 76.8 - start >= 5.0
        assert end - 76.8 <= 3.5
        assert dur >= 8.0
    finally:
        for k in (
            "MLBB_KILL_BANNER_MIN_PRE_SEC",
            "MLBB_KILL_BANNER_POST_SEC",
            "MLBB_FIGHT_MIN_SEC",
            "MLBB_FIGHT_MAX_SEC",
        ):
            os.environ.pop(k, None)


def test_presend_rejects_banner_tail_only(tmp_path: Path) -> None:
    from mlbb_vod_segment_feed import _validate_before_send

    vod = tmp_path / "vod.mp4"
    vod.write_bytes(b"x")
    rendered = tmp_path / "out.mp4"
    rendered.write_bytes(b"x" * 600_000)

    row = {
        "segment_id": "vid_73",
        "start": 73.0,
        "peak_start": 76.8,
        "banner_sec": 76.8,
        "banner_source": "ref",
        "kill_banner_tier": 2,
        "kill_banner": "double",
        "clip": {"input_duration": 10.0},
    }
    os.environ["MLBB_PRESEND_MIN_BANNER_LEAD"] = "10"
    os.environ["MLBB_KILL_BANNER_REQUIRED"] = "1"
    os.environ["MLBB_VOD_BANNER_PRESEND"] = "0"

    with (
        patch("mlbb_vod_segment_feed._segment_duration", return_value=10.0),
        patch("mlbb_vod_segment_feed._detect_render_freeze", return_value=(True, "freeze_ok", [])),
        patch("mlbb_vod_segment_feed._ffprobe_duration", return_value=10.0),
        patch(
            "gameplay_gate.score_segment_combat",
            return_value=(0.01, 0.01, 0.0, 0.0),
        ),
        patch("gameplay_gate.segment_looks_like_draft_or_queue", return_value=False),
        patch("gameplay_gate.segment_uniform_gameplay_ok", return_value=(True, "ok")),
        patch("visual_action_check.extract_and_check_segment", return_value={"visual_pass": True}),
        patch("mlbb_vod_segment_feed._vod_crop_box", return_value=None),
    ):
        ok, reason, _report = _validate_before_send(vod, row, rendered)
    assert ok is False
    assert "pre_banner" in reason
