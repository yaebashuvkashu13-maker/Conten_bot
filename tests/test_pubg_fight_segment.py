from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pubg_fight_segment as segmenter  # noqa: E402


def test_segmenter_expands_from_contact_through_finale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vod = tmp_path / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"vod")
    monkeypatch.setenv("PUBG_SEGMENT_SCAN_BEFORE", "18")
    monkeypatch.setenv("PUBG_SEGMENT_SCAN_AFTER", "24")
    monkeypatch.setenv("PUBG_SEGMENT_BIN_SEC", "2")
    monkeypatch.setenv("PUBG_SEGMENT_SAMPLE_SEC", "3")
    segmenter.clear_segment_cache()

    def timeline(_path: Path, scan_start: float, scan_end: float, *, step: float, sample: float):
        rows = []
        start = scan_start
        while start + sample <= scan_end:
            active = 92 <= start <= 108
            rows.append(
                {
                    "start": start,
                    "score": 0.8 if active else 0.05,
                    "gun": 0.08 if active else 0.0,
                }
            )
            start += step
        return rows

    monkeypatch.setattr(segmenter, "_activity_timeline", timeline)

    def killfeed(_path: Path, start: float, duration: float, _profile: str):
        return (0.8 if start <= 109 <= start + duration else 0.0), {}

    with patch("pubg_killfeed_ocr.score_killfeed_segment", side_effect=killfeed):
        start, duration, report = segmenter.resolve_pubg_fight_bounds(
            vod,
            100.0,
            file_duration=300.0,
        )

    assert start <= 95
    assert start + duration >= 110
    assert 10 <= duration <= 28
    assert report["kill_sec"] is not None
    assert report["segmenter"] == "pubg_fight_v1"
    assert report.get("knock_time") is not None
    assert float(report["knock_time"]) <= float(report["kill_sec"])
    assert report.get("loot_start") is not None
    assert float(report["loot_start"]) >= float(report["kill_sec"])


def test_segmenter_extends_past_notification_before_gunfire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill notification at peak; gunfire starts several seconds later."""
    vod = tmp_path / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"vod")
    monkeypatch.setenv("PUBG_SEGMENT_SCAN_BEFORE", "18")
    monkeypatch.setenv("PUBG_SEGMENT_SCAN_AFTER", "24")
    monkeypatch.setenv("PUBG_SEGMENT_BIN_SEC", "2")
    monkeypatch.setenv("PUBG_SEGMENT_SAMPLE_SEC", "3")
    monkeypatch.setenv("PUBG_SEGMENT_MIN_POST_PEAK_SEC", "14")
    monkeypatch.setenv("PUBG_SEGMENT_MAX_PREFLIGHT_SEC", "10")
    segmenter.clear_segment_cache()

    def timeline(_path: Path, scan_start: float, scan_end: float, *, step: float, sample: float):
        rows = []
        start = scan_start
        while start + sample <= scan_end:
            active = 108 <= start <= 118
            score = 0.85 if active else 0.04
            rows.append({"start": start, "score": score, "gun": 0.09 if active else 0.0})
            start += step
        return rows

    monkeypatch.setattr(segmenter, "_activity_timeline", timeline)

    def killfeed(_path: Path, start: float, duration: float, _profile: str):
        return (0.75, {"notification_samples": [{"index": 0, "score": 0.8}]})

    with patch("pubg_killfeed_ocr.score_killfeed_segment", side_effect=killfeed):
        start, duration, report = segmenter.resolve_pubg_fight_bounds(
            vod,
            100.0,
            file_duration=300.0,
        )

    assert start >= 90.0
    assert start + duration >= 114.0
    assert report["kill_sec"] is not None
    assert report["fight_end"] >= 114.0


def test_segmenter_trims_long_prefight_before_late_gunfire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Peak/notification early; sustained gunfire starts late — no 15s loot intro."""
    vod = tmp_path / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"vod")
    monkeypatch.setenv("PUBG_SEGMENT_SCAN_BEFORE", "18")
    monkeypatch.setenv("PUBG_SEGMENT_SCAN_AFTER", "24")
    monkeypatch.setenv("PUBG_SEGMENT_BIN_SEC", "2")
    monkeypatch.setenv("PUBG_SEGMENT_SAMPLE_SEC", "3")
    monkeypatch.setenv("PUBG_SEGMENT_MAX_PREFLIGHT_SEC", "6")
    segmenter.clear_segment_cache()

    def timeline(_path: Path, scan_start: float, scan_end: float, *, step: float, sample: float):
        rows = []
        start = scan_start
        while start + sample <= scan_end:
            gunfire = 134 <= start <= 158
            rows.append(
                {
                    "start": start,
                    "score": 0.82 if gunfire else (0.22 if 126 <= start < 134 else 0.04),
                    "gun": 0.09 if gunfire else 0.01,
                }
            )
            start += step
        return rows

    monkeypatch.setattr(segmenter, "_activity_timeline", timeline)

    with patch("pubg_killfeed_ocr.score_killfeed_segment", return_value=(0.7, {})):
        start, duration, report = segmenter.resolve_pubg_fight_bounds(
            vod,
            141.0,
            file_duration=600.0,
        )

    assert start >= 131.0
    assert (start - 141.0) / max(duration, 1.0) > -0.35
    assert start + duration >= 150.0
    assert report["shooting_start"] >= 133.0


def test_segmenter_fits_late_gunfire_span_not_cut_at_clip_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notification at peak; gunfire starts late and runs past old 24s scan tail."""
    vod = tmp_path / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"vod")
    monkeypatch.setenv("PUBG_SEGMENT_SCAN_BEFORE", "14")
    monkeypatch.setenv("PUBG_SEGMENT_SCAN_AFTER", "40")
    monkeypatch.setenv("PUBG_SEGMENT_BIN_SEC", "2")
    monkeypatch.setenv("PUBG_SEGMENT_SAMPLE_SEC", "3")
    segmenter.clear_segment_cache()

    def timeline(_path: Path, scan_start: float, scan_end: float, *, step: float, sample: float):
        rows = []
        start = scan_start
        while start + sample <= scan_end:
            gunfire = 876 <= start <= 894
            rows.append(
                {
                    "start": start,
                    "score": 0.86 if gunfire else 0.05,
                    "gun": 0.10 if gunfire else 0.0,
                }
            )
            start += step
        return rows

    monkeypatch.setattr(segmenter, "_activity_timeline", timeline)

    with patch("pubg_killfeed_ocr.score_killfeed_segment", return_value=(0.72, {})):
        start, duration, report = segmenter.resolve_pubg_fight_bounds(
            vod,
            858.0,
            file_duration=3600.0,
        )

    assert start >= 872.0
    assert start + duration >= 896.0
    rel = (876.0 - start) / max(duration, 1.0)
    assert rel < 0.35
    assert report["fight_end"] >= 896.0


def test_segmenter_falls_back_when_no_timeline(tmp_path: Path) -> None:
    vod = tmp_path / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"vod")
    segmenter.clear_segment_cache()
    with patch.object(segmenter, "_activity_timeline", return_value=[]):
        start, duration, report = segmenter.resolve_pubg_fight_bounds(
            vod,
            1.0,
            file_duration=2.0,
        )
    assert start == 0.0
    assert duration == 2.0
    assert report["fallback"] == "no_bins"
