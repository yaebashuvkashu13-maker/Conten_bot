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

    assert start < 92
    assert start + duration >= 110
    assert 10 <= duration <= 28
    assert report["kill_sec"] is not None
    assert report["segmenter"] == "pubg_fight_v1"


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
