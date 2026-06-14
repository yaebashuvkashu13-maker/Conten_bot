"""Scene library index for future montage / synthesis."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_make_scene_id_shorts() -> None:
    import mlbb_scene_library as lib

    assert lib.make_scene_id(source="youtube_shorts", video_id="abc12345678", start_sec=0.15) == "shorts:abc12345678:0.15"


def test_register_shorts_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    clip = tmp_path / "yt_test1234567.mp4"
    clip.write_bytes(b"x" * 50_000)

    monkeypatch.setenv("MLBB_DATA_ROOT", str(data))
    monkeypatch.setenv("MLBB_SCENE_LIBRARY", "1")

    import mlbb_scene_library as lib

    monkeypatch.setattr(lib, "index_path", lambda: data / "scene_library_index.jsonl")
    monkeypatch.setattr(lib, "stats_path", lambda: data / "scene_library_stats.json")
    monkeypatch.setattr(lib, "_probe_duration", lambda _p: 12.0)

    row = lib.register_shorts_label(
        path=clip,
        video_id="test1234567",
        is_good=True,
        row={"title": "savage fight", "score": 0.42, "clip_start_sec": 0.2},
    )
    assert row is not None
    assert row["scene_id"] == "shorts:test1234567:0.20"
    assert row["owner_label"] == "yes"
    assert row["scene_type"] == "kill"
    lines = (data / "scene_library_index.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    stats = json.loads((data / "scene_library_stats.json").read_text(encoding="utf-8"))
    assert stats["good"] == 1


def test_backfill_skips_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    shorts = tmp_path / "shorts"
    data.mkdir()
    shorts.mkdir()
    vid = "backfill12345"
    clip = shorts / f"yt_{vid}.mp4"
    clip.write_bytes(b"x" * 30_000)

    monkeypatch.setenv("MLBB_DATA_ROOT", str(data))
    monkeypatch.setenv("MLBB_SCENE_LIBRARY", "1")
    monkeypatch.setenv("MLBB_SHORTS_ROOT", str(shorts))
    monkeypatch.setenv("CONTENT_BOT_REPO", str(tmp_path))
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(tmp_path / "ex"))
    monkeypatch.setenv("MLBB_TRAINING_ARCHIVE_ROOT", str(tmp_path / "arch"))

    import mlbb_calibration_store as store
    import mlbb_scene_library as lib

    monkeypatch.setattr(store, "DATA_MLBB", data)
    monkeypatch.setattr(store, "SHORTS_ROOT", shorts)
    monkeypatch.setattr(store, "LABELS_PATH", data / "calibration_labels.json")
    monkeypatch.setattr(store, "INDEX_PATH", data / "youtube_shorts_index.json")
    monkeypatch.setattr(store, "FEED_SENT_PATH", data / "calibration_feed_sent.json")
    monkeypatch.setattr(store, "REPO_LABELS_PATH", tmp_path / "repo_labels.json")
    monkeypatch.setattr(lib, "index_path", lambda: data / "scene_library_index.jsonl")
    monkeypatch.setattr(lib, "stats_path", lambda: data / "scene_library_stats.json")
    monkeypatch.setattr(lib, "_probe_duration", lambda _p: 10.0)

    store.save_labels(
        {
            "good": [
                {
                    "video_id": vid,
                    "path": str(clip),
                    "title": "teamfight savage",
                    "score": 0.5,
                }
            ],
            "bad": [],
            "feedback": [],
        }
    )

    n1 = lib.backfill_from_labels(skip_existing=True)
    n2 = lib.backfill_from_labels(skip_existing=True)
    assert n1 == 1
    assert n2 == 0
