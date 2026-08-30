"""Integration tests for PUBG delivery path — must ship, not stall."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_quality import pubg_delivery_mode, pubg_quality_strict  # noqa: E402


def test_pubg_quality_strict_opt_in_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOD_PUBG_QUALITY_STRICT", raising=False)
    monkeypatch.setenv("VOD_PUBG_ONLY", "1")
    assert pubg_quality_strict() is False


def test_pubg_delivery_mode_with_delivery_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_DELIVERY_FIRST", "1")
    assert pubg_delivery_mode() is True


def test_montage_part_skips_metro_segment_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from shooter_vod_segment_feed import _validate_shooter_presend  # noqa: E402

    monkeypatch.setenv("PUBG_METRO_GATE", "1")
    monkeypatch.delenv("PUBG_METRO_SEGMENT_TRUST_VOD", raising=False)
    vod = Path("/tmp/fake.mp4")
    rendered = Path("/tmp/out.mp4")
    row = {"clip": {"start": 10.0, "output_duration": 14.0}, "peak_start": 12.0, "start": 10.0}

    with patch("pubg_metro_royale_gate.segment_looks_metro_royale") as seg_metro:
        with patch("pubg_shooting_gate.pubg_passes_shooting_gate", return_value=(True, "ok", {})):
            with patch("highlight_scorer.score_panns_audio", return_value={"panns_gun_max": 0.5}):
                with patch("shooter_author_kill_gate.author_kill_window_ok", return_value=(True, "ok", {})):
                    seg_metro.return_value = (False, "classic_outdoor_sky=2/3")
                    ok, reason, _ = _validate_shooter_presend(
                        "pubg", vod, row, rendered, montage_part=True
                    )
    seg_metro.assert_not_called()
    assert ok is True


def test_emergency_single_send_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from shooter_vod_segment_feed import _try_emergency_single_send  # noqa: E402

    monkeypatch.setenv("SHOOTER_VOD_DELIVERY_FIRST", "1")
    monkeypatch.setenv("VOD_PUBG_QUALITY_STRICT", "0")
    monkeypatch.setenv("DAILY_GAME_CYCLE_ENABLED", "0")
    vod = tmp_path / "yt_abc123xyz00.mp4"
    vod.write_bytes(b"\x00" * 128)
    out = tmp_path / "seg_out.mp4"
    out.write_bytes(b"\x00" * 64)

    with patch("shooter_vod_segment_feed._paths", return_value={"segments": tmp_path / "seg"}):
        with patch("shooter_vod_segment_feed._montage_limits", return_value=(1, 3, 55.0, 20.0, 70.0)):
            with patch("shooter_vod_segment_feed._prepare_montage_clip") as prep:
                with patch("shooter_vod_segment_feed.render_single_segment", return_value=True):
                    with patch(
                        "shooter_vod_segment_feed._validate_shooter_presend",
                        return_value=(True, "shoot_ok", {}),
                    ):
                        with patch("shooter_vod_segment_feed.send_video", return_value=True):
                            with patch("shooter_vod_segment_feed._ffprobe_duration", return_value=14.0):
                                with patch("shooter_vod_segment_feed.upsert_segment"):
                                    with patch("shooter_vod_segment_feed.mark_feed_sent"):
                                        with patch("shooter_vod_segment_feed.cycle_record_send"):
                                            prep.return_value = {
                                                "start": 10.0,
                                                "peak_start": 12.0,
                                                "input_duration": 14.0,
                                                "output_duration": 14.0,
                                            }
                                            n = _try_emergency_single_send(
                                                "pubg", "tok", "123", vod, [45.0, 120.0], "sig"
                                            )
    assert n == 1


def test_main_min_sec_not_600(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".video_bot.env"
    env_file.write_text("MLBB_VOD_MIN_SEC=180\nSHOOTER_VOD_DELIVERY_FIRST=1\n", encoding="utf-8")
    monkeypatch.setattr("shooter_vod_segment_feed.ENV_PATH", env_file)
    monkeypatch.setattr("shooter_vod_segment_feed._feed_lock", lambda _g: None)

    import shooter_vod_segment_feed as feed  # noqa: E402

    rc = feed.main()
    assert rc == 0
    assert os.environ.get("SHOOTER_VOD_MIN_SEC") != "600"


def test_auto_heal_triggers_on_bloated_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from vod_feed_recover import should_auto_heal  # noqa: E402
    from vod_game_registry import load_state  # noqa: E402

    root = tmp_path / "pubg"
    root.mkdir()
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps({"used_youtube_ids": [f"id{i:07d}aa"[:11] for i in range(300)], "vods": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    monkeypatch.setenv("SHOOTER_VOD_USED_IDS_MAX", "100")
    ok, reason = should_auto_heal("pubg", load_state("pubg"))
    assert ok is True
    assert "used_ids" in reason


import os  # noqa: E402
