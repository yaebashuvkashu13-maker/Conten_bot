from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from highlight_scorer import (  # noqa: E402
    _filter_bad_label_starts,
    _labels_for_vod,
    segment_overlaps_owner_label,
)


def test_labels_for_vod_merges_vod_segment_store(monkeypatch, tmp_path: Path) -> None:
    data_root = tmp_path / "mlbb"
    data_root.mkdir()
    owner_path = data_root / "mobile_legends_owner_labels.json"
    vseg_path = data_root / "vod_segment_labels.json"

    owner_path.write_text(
        json.dumps({"videos": {"testvid": [{"time_sec": 100, "label": "good"}]}}),
        encoding="utf-8",
    )
    vseg_path.write_text(
        json.dumps(
            {
                "good": [],
                "bad": [{"segment_id": "testvid_500", "start": 500}],
                "feedback": [],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("MLBB_OWNER_LABELS_PATH", str(owner_path))
    monkeypatch.setenv("MLBB_VOD_SEGMENT_LABELS", str(vseg_path))

    vod = tmp_path / "yt_testvid.mp4"
    vod.write_bytes(b"")
    rows = _labels_for_vod(vod, "mobile_legends")
    assert len(rows) == 2
    assert {r["label"] for r in rows} == {"good", "bad"}


def test_bad_label_blocks_with_90s_pad(monkeypatch, tmp_path: Path) -> None:
    data_root = tmp_path / "mlbb"
    data_root.mkdir()
    vseg_path = data_root / "vod_segment_labels.json"
    vseg_path.write_text(
        json.dumps(
            {
                "good": [],
                "bad": [{"segment_id": "testvid_600", "start": 600, "by_chat": "123", "reason": "boring"}],
                "feedback": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MLBB_VOD_SEGMENT_LABELS", str(vseg_path))
    monkeypatch.setenv("HIGHLIGHT_OWNER_BAD_PAD_SEC", "90")

    vod = tmp_path / "yt_testvid.mp4"
    vod.write_bytes(b"")

    assert segment_overlaps_owner_label(vod, 520, 15, "mobile_legends", label="bad", pad_sec=90)
    assert not segment_overlaps_owner_label(vod, 400, 15, "mobile_legends", label="bad", pad_sec=90)

    starts = _filter_bad_label_starts(vod, "mobile_legends", [520.0, 200.0])
    assert 520.0 not in starts
    assert 200.0 in starts


def test_auto_boring_bad_does_not_block_rescan(monkeypatch, tmp_path: Path) -> None:
    data_root = tmp_path / "mlbb"
    data_root.mkdir()
    owner_path = data_root / "mobile_legends_owner_labels.json"
    owner_path.write_text(
        json.dumps(
            {
                "videos": {
                    "testvid": [
                        {
                            "time_sec": 312.0,
                            "label": "bad",
                            "source": "vod_segment_backfill",
                            "note": "boring",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MLBB_OWNER_LABELS_PATH", str(owner_path))

    vod = tmp_path / "yt_testvid.mp4"
    vod.write_bytes(b"")

    assert not segment_overlaps_owner_label(vod, 290, 15, "mobile_legends", label="bad", pad_sec=90)
