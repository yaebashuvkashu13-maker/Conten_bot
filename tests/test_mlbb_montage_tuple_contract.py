"""Montage send must return a 4-tuple — 3-tuple unpack crashed the feed for hours."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import mlbb_vod_segment_feed as feed  # noqa: E402


def test_skip_is_sticky_solo_multi() -> None:
    assert feed._skip_is_sticky("8LN_404:solo_needs_live_multi=0:need>=2")
    assert feed._skip_is_sticky("x:presend_banner_floor")
    assert not feed._skip_is_sticky("x:render_fail")
    assert not feed._skip_is_sticky("x:motion_low")


def test_montage_abort_returns_four_tuple(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vod = tmp_path / "yt_fake.mp4"
    vod.write_bytes(b"0" * 100)

    rows = [
        {
            "segment_id": "fake_100",
            "start": 100,
            "peak_start": 108,
            "kill_banner_tier": 1,
            "kill_banner": "single",
            "fight_dur": 12,
            "clip": {"start": 100, "input_duration": 12},
        },
        {
            "segment_id": "fake_200",
            "start": 200,
            "peak_start": 208,
            "kill_banner_tier": 1,
            "kill_banner": "single",
            "fight_dur": 12,
            "clip": {"start": 200, "input_duration": 12},
        },
    ]

    monkeypatch.setattr(feed, "render_single_segment", lambda *a, **k: True)
    monkeypatch.setattr(
        feed,
        "_validate_before_send",
        lambda *a, **k: (False, "solo_needs_live_multi=0:need>=2", {}),
    )
    monkeypatch.setattr(feed, "vod_youtube_id", lambda p: "fake")
    monkeypatch.setattr(feed, "segments_root", lambda: tmp_path)
    monkeypatch.setattr(
        "mlbb_vod_montage.cleanup_temps",
        lambda temps: None,
    )

    with patch("mlbb_vod_montage.build_montage_id", return_value="fake_m"):
        with patch("smart_video_editor.ffprobe_duration", return_value=12.0):
            out = feed._send_montage_batch("t", "c", vod, rows, "sig")

    assert len(out) == 4
    sent, skipped, blocked, permanent = out
    assert sent == 0
    assert skipped == 2
    assert blocked == 0
    assert permanent is True


def test_segment_batch_propagates_montage_permanent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vod = tmp_path / "yt_fake.mp4"
    vod.write_bytes(b"0" * 100)
    rows = [
        {"segment_id": "a", "kill_banner_tier": 2, "start": 1, "peak_start": 2},
        {"segment_id": "b", "kill_banner_tier": 2, "start": 40, "peak_start": 42},
    ]
    monkeypatch.setenv("MLBB_VOD_MONTAGE", "1")
    monkeypatch.setenv("MLBB_SKIP_MONTAGE", "0")
    monkeypatch.setattr(
        feed,
        "_send_montage_batch",
        lambda *a, **k: (0, 2, 0, True),
    )
    monkeypatch.setattr(
        "mlbb_vod_montage.montage_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "mlbb_vod_montage.pick_montage_rows",
        lambda rows: rows,
    )
    monkeypatch.setattr(
        "mlbb_learning_first.can_send",
        lambda n: (True, ""),
    )
    monkeypatch.setattr("mlbb_learning_first.daily_send_count", lambda: 0)
    monkeypatch.setattr("mlbb_learning_first.max_daily_sends", lambda: 99)

    n, sk, bl, permanent = feed._send_segment_batch("t", "c", vod, rows, "sig")
    assert (n, sk, bl, permanent) == (0, 2, 0, True)


def test_single_fallback_returns_four_tuple(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vod = tmp_path / "yt_fake.mp4"
    vod.write_bytes(b"0" * 100)
    row = {"segment_id": "a", "kill_banner_tier": 2, "start": 1, "peak_start": 2}

    monkeypatch.setattr(
        feed,
        "_send_segment_batch",
        lambda *a, **k: (0, 1, 0, True),
    )
    out = feed._send_single_fallback("t", "c", vod, row, "sig")
    assert out == (0, 1, 0, True)
    # Caller must unpack four values without ValueError (the 3.5h crash).
    n, sk, bl, permanent = out
    assert permanent is True
