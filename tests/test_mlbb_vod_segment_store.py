from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mlbb_vod_segment_store import (  # noqa: E402
    append_owner_label_json,
    apply_owner_label,
    backfill_owner_labels_from_vod_segments,
    load_owner_labels_json,
    upsert_segment,
)


def test_append_owner_label_json_dedupes(monkeypatch, tmp_path: Path) -> None:
    data_root = tmp_path / "mlbb"
    data_root.mkdir()
    owner_path = data_root / "mobile_legends_owner_labels.json"
    monkeypatch.setenv("CONTENT_BOT_REPO", str(tmp_path))
    monkeypatch.setenv("MLBB_DATA_ROOT", str(data_root))
    monkeypatch.setenv("MLBB_OWNER_LABELS_PATH", str(owner_path))

    append_owner_label_json("abc123vid01", 1523.0, "bad", note="spawn")
    append_owner_label_json("abc123vid01", 1523.0, "bad", note="duplicate")
    data = load_owner_labels_json()
    rows = data["videos"]["abc123vid01"]
    assert len(rows) == 1
    assert rows[0]["label"] == "bad"


def test_apply_owner_label_syncs_owner_json(monkeypatch, tmp_path: Path) -> None:
    data_root = tmp_path / "mlbb"
    seg_root = tmp_path / "segments"
    data_root.mkdir()
    seg_root.mkdir()
    owner_path = data_root / "mobile_legends_owner_labels.json"
    labels_path = data_root / "vod_segment_labels.json"
    index_path = data_root / "vod_segment_index.json"

    monkeypatch.setenv("CONTENT_BOT_REPO", str(tmp_path))
    monkeypatch.setenv("MLBB_DATA_ROOT", str(data_root))
    monkeypatch.setenv("MLBB_OWNER_LABELS_PATH", str(owner_path))
    monkeypatch.setenv("MLBB_VOD_SEGMENT_LABELS", str(labels_path))
    monkeypatch.setenv("MLBB_VOD_SEGMENT_INDEX", str(index_path))
    monkeypatch.setenv("MLBB_VOD_SEGMENTS_ROOT", str(seg_root))
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(tmp_path / "exemplars"))

    clip = seg_root / "seg_abcdefghijk_1000.mp4"
    clip.write_bytes(b"fake")

    upsert_segment(
        {
            "segment_id": "abcdefghijk_1000",
            "path": str(clip),
            "vod": str(tmp_path / "yt_abcdefghijk.mp4"),
            "start": 996,
            "peak_start": 1000,
            "score": 0.8,
        }
    )
    labels_path.write_text(json.dumps({"good": [], "bad": [], "feedback": []}), encoding="utf-8")

    ok, label = apply_owner_label("abcdefghijk_1000", is_good=False, reason="freeze")
    assert ok is True
    assert label == "bad"

    owner = load_owner_labels_json()
    assert owner["videos"]["abcdefghijk"][0]["time_sec"] == 1000.0
    assert owner["videos"]["abcdefghijk"][0]["label"] == "bad"


def test_backfill_from_vod_segments(monkeypatch, tmp_path: Path) -> None:
    data_root = tmp_path / "mlbb"
    data_root.mkdir()
    owner_path = data_root / "mobile_legends_owner_labels.json"
    labels_path = data_root / "vod_segment_labels.json"
    index_path = data_root / "vod_segment_index.json"

    monkeypatch.setenv("CONTENT_BOT_REPO", str(tmp_path))
    monkeypatch.setenv("MLBB_DATA_ROOT", str(data_root))
    monkeypatch.setenv("MLBB_OWNER_LABELS_PATH", str(owner_path))
    monkeypatch.setenv("MLBB_VOD_SEGMENT_LABELS", str(labels_path))
    monkeypatch.setenv("MLBB_VOD_SEGMENT_INDEX", str(index_path))

    labels_path.write_text(
        json.dumps(
            {
                "good": [{"segment_id": "vid11chars_500", "start": 496, "vod": ""}],
                "bad": [{"segment_id": "vid11chars_900", "start": 896, "vod": ""}],
                "feedback": [],
            }
        ),
        encoding="utf-8",
    )
    index_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"segment_id": "vid11chars_500", "peak_start": 500},
                    {"segment_id": "vid11chars_900", "peak_start": 900},
                ]
            }
        ),
        encoding="utf-8",
    )

    added = backfill_owner_labels_from_vod_segments()
    assert added == 2
    owner = load_owner_labels_json()
    assert len(owner["videos"]["vid11chars"]) == 2
