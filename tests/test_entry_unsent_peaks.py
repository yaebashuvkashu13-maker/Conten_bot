"""Dict last_pool_peaks must count as remaining, not mined-out."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_entry_unsent_peak_count_dict_peaks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    root.mkdir()
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    (root / "vod_segment_feed_sent.json").write_text(
        json.dumps({"sent": ["abc123xyz00_100"], "updated_at": "2026-09-04 05:00:00"}),
        encoding="utf-8",
    )
    (root / "vod_segment_index.json").write_text(json.dumps({"segments": []}), encoding="utf-8")

    import shooter_vod_segment_feed as feed

    entry = {
        "id": "abc123xyz00",
        "last_pool_peaks": [
            {"peak_sec": 100.0, "score": 0.0, "blocked_reason": ""},
            {"peak_sec": 250.0, "score": 0.0, "blocked_reason": ""},
        ],
    }
    assert feed._entry_unsent_peak_count("pubg", entry) == 1


def test_mark_exhausted_preserves_pool_peaks() -> None:
    import shooter_vod_segment_feed as feed
    from pathlib import Path

    state = {
        "vods": [
            {
                "id": "abc123xyz00",
                "path": "/tmp/yt_abc123xyz00.mp4",
                "last_pool_peaks": [{"peak_sec": 12.0}],
            }
        ]
    }
    feed._mark_vod_exhausted(state, Path("/tmp/yt_abc123xyz00.mp4"), reason="pubg_mined_out")
    assert state["vods"][0]["exhausted"] is True
    assert state["vods"][0]["last_pool_peaks"] == [{"peak_sec": 12.0}]
