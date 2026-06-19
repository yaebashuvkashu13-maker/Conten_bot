"""Tests for MLBB calibration index rebuild and freshness filter."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_calibration_store import (  # noqa: E402
    DISLIKE_REASONS,
    claim_feed_candidates,
    dislike_reason_keyboard_markup,
    is_fresh_short,
    rebuild_index_from_disk,
)


def test_rebuild_index_from_disk(tmp_path: Path, monkeypatch) -> None:
    shorts = tmp_path / "shorts"
    shorts.mkdir()
    mp4 = shorts / "yt_abcdefghijk.mp4"
    mp4.write_bytes(b"x" * 20_000)
    index = tmp_path / "index.json"
    labels = tmp_path / "labels.json"
    index.write_text(json.dumps({"candidates": []}))
    labels.write_text(json.dumps({"good": [], "bad": [], "feedback": []}))

    monkeypatch.setenv("MLBB_SHORTS_ROOT", str(shorts))
    monkeypatch.setenv("MLBB_SHORTS_INDEX", str(index))
    monkeypatch.setenv("MLBB_CALIBRATION_LABELS", str(labels))

    import mlbb_calibration_store as store

    monkeypatch.setattr(store, "SHORTS_ROOT", shorts)
    monkeypatch.setattr(store, "INDEX_PATH", index)
    monkeypatch.setattr(store, "LABELS_PATH", labels)

    n = rebuild_index_from_disk()
    assert n == 1
    data = json.loads(index.read_text())
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["video_id"] == "abcdefghijk"
    assert data["candidates"][0].get("ingested_at")


def test_rebuild_backfills_ingested_at_for_existing_row(tmp_path: Path, monkeypatch) -> None:
    shorts = tmp_path / "shorts"
    shorts.mkdir()
    mp4 = shorts / "yt_abcdefghijk.mp4"
    mp4.write_bytes(b"x" * 20_000)
    index = tmp_path / "index.json"
    labels = tmp_path / "labels.json"
    index.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "video_id": "abcdefghijk",
                        "id": "abcdefghijk",
                        "path": str(mp4),
                        "title": "abcdefghijk",
                        "score": 0.0,
                    }
                ]
            }
        )
    )
    labels.write_text(json.dumps({"good": [], "bad": [], "feedback": []}))

    monkeypatch.setenv("MLBB_SHORTS_ROOT", str(shorts))
    monkeypatch.setenv("MLBB_SHORTS_INDEX", str(index))
    monkeypatch.setenv("MLBB_CALIBRATION_LABELS", str(labels))

    import mlbb_calibration_store as store

    monkeypatch.setattr(store, "SHORTS_ROOT", shorts)
    monkeypatch.setattr(store, "INDEX_PATH", index)
    monkeypatch.setattr(store, "LABELS_PATH", labels)

    rebuild_index_from_disk()
    row = json.loads(index.read_text())["candidates"][0]
    assert row.get("ingested_at")


def test_is_fresh_short_rejects_old_and_unknown(monkeypatch) -> None:
    recent = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y%m%d")
    monkeypatch.setenv("MLBB_SHORTS_MIN_YEAR", "2024")
    monkeypatch.setenv("MLBB_SHORTS_DAYS", "60")
    monkeypatch.setenv("MLBB_SHORTS_REQUIRE_DATE", "1")

    assert is_fresh_short({"upload_date": recent})
    assert not is_fresh_short({"upload_date": "20200315"})
    assert not is_fresh_short({"upload_date": ""})
    assert not is_fresh_short({"source": "disk_rebuild"})
    assert is_fresh_short({"ingested_at": time.strftime("%Y-%m-%d %H:%M:%S")})


def test_dislike_reason_keyboard_has_eight_buttons() -> None:
    markup = dislike_reason_keyboard_markup("abcdefghijk")
    buttons = [b for row in markup["inline_keyboard"] for b in row]
    assert len(buttons) == 8
    assert len(DISLIKE_REASONS) == 8
    for btn in buttons:
        assert len(btn["callback_data"]) <= 64
        assert btn["callback_data"].startswith("mlbb_bad:abcdefghijk:")


def test_claim_feed_candidates_prevents_duplicate(tmp_path: Path, monkeypatch) -> None:
    shorts = tmp_path / "shorts"
    shorts.mkdir()
    mp4 = shorts / "yt_abcdefghijk.mp4"
    mp4.write_bytes(b"x" * 20_000)
    sent = tmp_path / "sent.json"
    proc_lock = tmp_path / "proc.lock"
    sent_lock = tmp_path / "sent.lock"

    monkeypatch.setenv("MLBB_FEED_SENT", str(sent))
    monkeypatch.setenv("MLBB_FEED_LOCK", str(proc_lock))
    monkeypatch.setenv("MLBB_FEED_SENT_LOCK", str(sent_lock))

    import mlbb_calibration_store as store

    monkeypatch.setattr(store, "FEED_SENT_PATH", sent)
    monkeypatch.setattr(store, "FEED_PROC_LOCK_PATH", proc_lock)
    monkeypatch.setattr(store, "FEED_SENT_LOCK_PATH", sent_lock)

    row = {"video_id": "abcdefghijk", "path": str(mp4)}
    first = claim_feed_candidates([row])
    second = claim_feed_candidates([row])
    assert len(first) == 1
    assert len(second) == 0
