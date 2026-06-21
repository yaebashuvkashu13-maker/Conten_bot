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
    apply_owner_label,
    claim_feed_candidates,
    claimed_count,
    dislike_reason_keyboard_markup,
    is_fresh_short,
    load_index,
    load_labels,
    mark_feed_blocked,
    owner_rank_enabled,
    pending_candidates,
    purge_non_mlbb_candidates,
    rebuild_index_from_disk,
    release_feed_claims,
    release_stale_claims,
    save_labels,
    title_blocked_by_owner_feedback,
    upsert_candidate,
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


def test_labeled_keyboard_good_has_hq_button() -> None:
    from mlbb_calibration_store import labeled_keyboard_markup

    markup = labeled_keyboard_markup("good", video_id="abcdefghijk")
    flat = [b for row in markup["inline_keyboard"] for b in row]
    assert any(b["callback_data"] == "mlbb_hq:abcdefghijk" for b in flat)


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


def test_release_feed_claims_restores_queue(tmp_path: Path, monkeypatch) -> None:
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
    assert len(claim_feed_candidates([row])) == 1
    assert release_feed_claims(["abcdefghijk"]) >= 1
    assert len(claim_feed_candidates([row])) == 1


def test_claimed_count_and_stale_release(tmp_path: Path, monkeypatch) -> None:
    sent = tmp_path / "sent.json"
    sent.write_text(
        json.dumps(
            {
                "sent_ids": [],
                "sent_file_ids": [],
                "claimed_ids": {"abcdefghijk": "2000-01-01 00:00:00"},
            }
        )
    )
    monkeypatch.setenv("MLBB_FEED_SENT", str(sent))
    monkeypatch.setenv("MLBB_FEED_SENT_LOCK", str(tmp_path / "sent.lock"))

    import mlbb_calibration_store as store

    monkeypatch.setattr(store, "FEED_SENT_PATH", sent)
    monkeypatch.setattr(store, "FEED_SENT_LOCK_PATH", tmp_path / "sent.lock")

    assert claimed_count() == 1
    assert release_stale_claims(max_age_sec=60) == 1
    assert claimed_count() == 0


def test_mark_feed_blocked_sets_gameplay_pass_zero(tmp_path: Path, monkeypatch) -> None:
    shorts = tmp_path / "shorts"
    shorts.mkdir()
    mp4 = shorts / "yt_abcdefghijk.mp4"
    mp4.write_bytes(b"x" * 20_000)
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "video_id": "abcdefghijk",
                        "path": str(mp4),
                        "gameplay_pass": 1,
                        "gameplay_score": 0.9,
                        "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                ]
            }
        )
    )
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"good": [], "bad": [], "feedback": []}))
    sent = tmp_path / "sent.json"
    sent.write_text(json.dumps({"sent_ids": [], "sent_file_ids": [], "claimed_ids": {}}))

    monkeypatch.setenv("MLBB_SHORTS_ROOT", str(shorts))
    monkeypatch.setenv("MLBB_SHORTS_INDEX", str(index))
    monkeypatch.setenv("MLBB_CALIBRATION_LABELS", str(labels))
    monkeypatch.setenv("MLBB_FEED_SENT", str(sent))
    monkeypatch.setenv("MLBB_SHORTS_REQUIRE_DATE", "0")

    import mlbb_calibration_store as store

    monkeypatch.setattr(store, "SHORTS_ROOT", shorts)
    monkeypatch.setattr(store, "INDEX_PATH", index)
    monkeypatch.setattr(store, "LABELS_PATH", labels)
    monkeypatch.setattr(store, "FEED_SENT_PATH", sent)

    assert len(pending_candidates(limit=10, repair=False)) == 1
    mark_feed_blocked("abcdefghijk", reason="not_gameplay", score=0.1)
    assert len(pending_candidates(limit=10, repair=False)) == 0
    row = load_index()["candidates"][0]
    assert row["gameplay_pass"] == 0
    assert row["gameplay_reason"] == "not_gameplay"


def test_pending_sorts_by_owner_score_when_enabled(tmp_path: Path, monkeypatch) -> None:
    shorts = tmp_path / "shorts"
    shorts.mkdir()
    ex_root = tmp_path / "exemplars"
    (ex_root / "mobile_legends" / "good").mkdir(parents=True)
    (ex_root / "mobile_legends" / "bad").mkdir(parents=True)
    for i, score in (("aaa11111111", 0.9), ("bbb22222222", 0.1)):
        mp4 = shorts / f"yt_{i}.mp4"
        mp4.write_bytes(b"x" * 20_000)
        (ex_root / "mobile_legends" / "good" / f"cal_{i}.mp4").write_bytes(b"x" * 1000)
    index = tmp_path / "index.json"
    labels = tmp_path / "labels.json"
    sent = tmp_path / "sent.json"
    recent = "20260601"
    older = "20260101"
    index.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "video_id": "aaa11111111",
                        "path": str(shorts / "yt_aaa11111111.mp4"),
                        "gameplay_pass": 1,
                        "gameplay_score": 0.8,
                        "upload_date": older,
                        "owner_score": 0.9,
                    },
                    {
                        "video_id": "bbb22222222",
                        "path": str(shorts / "yt_bbb22222222.mp4"),
                        "gameplay_pass": 1,
                        "gameplay_score": 0.8,
                        "upload_date": recent,
                        "owner_score": 0.1,
                    },
                ]
            }
        )
    )
    labels.write_text(json.dumps({"good": [], "bad": [], "feedback": []}))
    sent.write_text(json.dumps({"sent_ids": [], "sent_file_ids": [], "claimed_ids": {}}))

    monkeypatch.setenv("MLBB_SHORTS_ROOT", str(shorts))
    monkeypatch.setenv("MLBB_SHORTS_INDEX", str(index))
    monkeypatch.setenv("MLBB_CALIBRATION_LABELS", str(labels))
    monkeypatch.setenv("MLBB_FEED_SENT", str(sent))
    monkeypatch.setenv("MLBB_SHORTS_REQUIRE_DATE", "0")
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(ex_root))
    monkeypatch.setenv("MLBB_OWNER_MIN_EXEMPLARS", "2")

    import mlbb_calibration_store as store

    monkeypatch.setattr(store, "SHORTS_ROOT", shorts)
    monkeypatch.setattr(store, "INDEX_PATH", index)
    monkeypatch.setattr(store, "LABELS_PATH", labels)
    monkeypatch.setattr(store, "FEED_SENT_PATH", sent)
    monkeypatch.setattr(store, "EXEMPLAR_ROOT", ex_root)

    assert owner_rank_enabled()
    rows = pending_candidates(limit=2, repair=False)
    assert [r["video_id"] for r in rows] == ["aaa11111111", "bbb22222222"]


def test_title_blocked_by_owner_not_gameplay(tmp_path: Path, monkeypatch) -> None:
    labels = tmp_path / "labels.json"
    labels.write_text(
        json.dumps(
            {
                "good": [],
                "bad": [
                    {
                        "video_id": "kk0OccNvDrw",
                        "title": "Penn State Football hype video",
                        "reason": "not_gameplay",
                    }
                ],
                "feedback": [],
            }
        )
    )
    monkeypatch.setenv("MLBB_CALIBRATION_LABELS", str(labels))
    import mlbb_calibration_store as store

    monkeypatch.setattr(store, "LABELS_PATH", labels)
    reason = title_blocked_by_owner_feedback("New Penn State Football clip")
    assert reason in ("non_mlbb_sports", "owner_not_gameplay:football")


def test_apply_owner_label_bad_blocks_queue(tmp_path: Path, monkeypatch) -> None:
    shorts = tmp_path / "shorts"
    shorts.mkdir()
    mp4 = shorts / "yt_abcdefghijk.mp4"
    mp4.write_bytes(b"x" * 20_000)
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "video_id": "abcdefghijk",
                        "path": str(mp4),
                        "title": "test",
                        "gameplay_pass": 1,
                        "gameplay_score": 0.9,
                        "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                ]
            }
        )
    )
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"good": [], "bad": [], "feedback": []}))

    monkeypatch.setenv("MLBB_SHORTS_ROOT", str(shorts))
    monkeypatch.setenv("MLBB_SHORTS_INDEX", str(index))
    monkeypatch.setenv("MLBB_CALIBRATION_LABELS", str(labels))
    monkeypatch.setenv("MLBB_SHORTS_REQUIRE_DATE", "0")

    import mlbb_calibration_store as store

    monkeypatch.setattr(store, "SHORTS_ROOT", shorts)
    monkeypatch.setattr(store, "INDEX_PATH", index)
    monkeypatch.setattr(store, "LABELS_PATH", labels)
    ex_root = tmp_path / "exemplars"
    owner_labels = tmp_path / "owner_labels.json"
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(ex_root))
    monkeypatch.setenv("MLBB_OWNER_LABELS_PATH", str(owner_labels))
    monkeypatch.setattr(store, "EXEMPLAR_ROOT", ex_root)

    ok, label = apply_owner_label("abcdefghijk", is_good=False, reason="not_gameplay")
    assert ok and label == "bad"
    row = load_index()["candidates"][0]
    assert row["gameplay_pass"] == 0
    assert row["gameplay_reason"] == "not_gameplay"
