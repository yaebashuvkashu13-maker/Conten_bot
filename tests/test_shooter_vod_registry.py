"""Registry lookup / duplicate exhaust for shooter VOD feed."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import shooter_vod_segment_feed as feed  # noqa: E402


def test_vod_registry_entry_prefers_path_over_stale_id(tmp_path: Path) -> None:
    vod = tmp_path / "yt_QDda58YJxUY.mp4"
    vod.write_bytes(b"x")
    state = {
        "vods": [
            {
                "id": "QDda58YJxUY",
                "path": "",
                "exhausted": True,
                "reject_reason": "no_combat_peaks",
                "file_deleted": True,
                "last_pool_peaks": [{"peak_sec": 286.0, "score": 1.0}],
                "last_scan_blocked": True,
            },
            {
                "id": "QDda58YJxUY",
                "path": str(vod),
                "exhausted": False,
                "last_pool_peaks": [{"peak_sec": 286.0, "score": 1.0}],
            },
        ]
    }
    entry = feed._vod_registry_entry(state, vod)
    assert entry is not None
    assert entry.get("path") == str(vod)
    assert entry.get("exhausted") is False


def test_youtube_id_permanently_spent_with_duplicate_rows(tmp_path: Path) -> None:
    vod = tmp_path / "yt_QDda58YJxUY.mp4"
    state = {
        "vods": [
            {
                "id": "QDda58YJxUY",
                "path": "",
                "exhausted": True,
                "reject_reason": "no_combat_peaks",
                "last_pool_peaks": [{"peak_sec": 286.0}],
                "last_scan_blocked": True,
                "file_deleted": True,
            },
            {
                "id": "QDda58YJxUY",
                "path": str(vod),
                "exhausted": False,
            },
        ]
    }
    assert feed._youtube_id_permanently_spent(state, "QDda58YJxUY") is True


def test_mark_siblings_exhausted_updates_all_rows() -> None:
    state = {
        "vods": [
            {"id": "abcdefghijk", "path": "", "exhausted": True, "reject_reason": "no_combat_peaks"},
            {"id": "abcdefghijk", "path": "/tmp/yt_abcdefghijk.mp4", "exhausted": False},
        ]
    }
    primary = state["vods"][1]
    primary["last_scan_blocked"] = True
    primary["last_pool_peaks"] = [{"peak_sec": 10.0}]
    touched = feed._mark_siblings_exhausted(
        state,
        youtube_id="abcdefghijk",
        reject_reason="all_peaks_blocked",
        primary=primary,
    )
    assert len(touched) == 2
    for row in state["vods"]:
        assert row["exhausted"] is True
        assert row["reject_reason"] == "all_peaks_blocked"
        assert row["last_scan_blocked"] is True
