"""Tests for montage keyboard, PANNs cache, killfeed ranking."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_montage_keyboard_per_part() -> None:
    from shooter_vod_segment_store import montage_keyboard_markup

    parts = [
        {"segment_id": "abc_100", "peak_start": 100.0},
        {"segment_id": "abc_200", "peak_start": 200.0},
    ]
    kb = montage_keyboard_markup("pubg", parts)
    rows = kb["inline_keyboard"]
    assert len(rows) == 2
    assert "abc_100" in rows[0][0]["callback_data"]
    assert "abc_200" in rows[1][0]["callback_data"]


def test_montage_parts_from_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    index = root / "vod_segment_index.json"
    index.parent.mkdir(parents=True)
    index.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "segment_id": "abc_100",
                        "montage_parts": ["abc_100", "abc_200"],
                        "path": "/tmp/m.mp4",
                    },
                    {"segment_id": "abc_200", "peak_start": 200, "path": "/tmp/m.mp4"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    from shooter_vod_segment_store import montage_parts_from_segment

    parts = montage_parts_from_segment("pubg", "abc_100")
    assert len(parts) == 2
    assert parts[0]["segment_id"] == "abc_100"


def test_panns_audio_cache_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vod = tmp_path / "yt_test1234567.mp4"
    vod.write_bytes(b"fake")
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("PANN_AUDIO_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("PANN_AUDIO_CACHE", "1")

    from panns_audio_cache import get_cached, put_cached

    scores = {"panns_gun_max": 0.42, "panns_gunshot": 0.4}
    put_cached(vod, 10.0, 8.0, scores)
    hit = get_cached(vod, 10.0, 8.0)
    assert hit is not None
    assert hit["panns_gun_max"] == pytest.approx(0.42)


def test_panns_prewarm_uses_all_offsets(monkeypatch: pytest.MonkeyPatch) -> None:
    from panns_audio_cache import prewarm_grid
    from unittest.mock import patch

    monkeypatch.setenv("PANN_PREWARM_WORKERS", "4")
    offsets = [10.0, 20.0, 30.0, 40.0]
    with patch("highlight_scorer.score_panns_audio", return_value={}) as scorer:
        assert prewarm_grid(Path("/tmp/vod.mp4"), offsets, 8.0) == 4
    assert sorted(call.args[1] for call in scorer.call_args_list) == offsets


def test_killfeed_rank_preserves_all_peaks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_KILLFEED_RANK", "1")

    from pubg_killfeed_ocr import rank_peaks_by_killfeed

    peaks = [100.0, 200.0, 300.0]

    def fake_score(_path, _start, _dur, _profile):
        if _start < 150:
            return 0.8, {}
        return 0.1, {}

    monkeypatch.setattr("pubg_killfeed_ocr.score_killfeed_segment", fake_score)
    ranked, reason = rank_peaks_by_killfeed(Path("/tmp/x.mp4"), peaks, "pubg")
    assert set(ranked) == set(peaks)
    assert ranked[0] == 100.0
    assert "killfeed_rank" in reason


def test_pubg_killfeed_uses_movable_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    from pubg_killfeed_ocr import score_killfeed_segment

    monkeypatch.setenv("PUBG_KILL_NOTIFICATION_AUTO", "1")
    fake = (
        0.72,
        {
            "notification_score": 0.72,
            "notification_text": "PlayerA AKM PlayerB",
            "notification_box": [0.1, 0.2, 0.4, 0.05],
        },
    )
    monkeypatch.setattr(
        "pubg_notification_cache.cached_score_kill_notification_segment",
        lambda *_args, **_kwargs: fake,
    )
    score, report = score_killfeed_segment(Path("/tmp/vod.mp4"), 10, 14, "pubg")
    assert score == pytest.approx(0.72)
    assert report["notification_box"][0] == 0.1
