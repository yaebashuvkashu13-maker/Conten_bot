"""Tests for learned MLBB uploader/query source ranking."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_source_yield import (  # noqa: E402
    record_owner_feedback,
    record_vod_outcome,
    source_rank_adjustment,
    uploader_hard_blocked,
)


def _meta(video_id: str, uploader: str, query: str) -> dict:
    return {
        "id": video_id,
        "uploader": uploader,
        "search_query": query,
        "title": "MLBB Savage Ranked Full Match",
    }


def test_source_rank_learns_owner_approved_uploader(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_SOURCE_YIELD_PATH", str(tmp_path / "yield.json"))
    good = _meta("goodvideo01", "Good Channel", "mlbb savage")
    weak = _meta("weakvideo01", "Weak Channel", "mlbb ranked")

    record_vod_outcome(good, sent=1)
    record_owner_feedback("goodvideo01", label="good", item_id="seg-good")
    record_vod_outcome(weak, sent=0)

    assert source_rank_adjustment(good) > source_rank_adjustment(weak)


def test_uploader_not_blocked_after_one_empty_vod(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_SOURCE_YIELD_PATH", str(tmp_path / "yield.json"))
    meta = _meta("emptyvideo1", "New Channel", "mlbb ranked")
    record_vod_outcome(meta, sent=0)
    assert uploader_hard_blocked(meta) is False


def test_uploader_block_requires_repeated_failure_and_bad_feedback(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MLBB_SOURCE_YIELD_PATH", str(tmp_path / "yield.json"))
    monkeypatch.setenv("MLBB_SOURCE_BLOCK_MIN_VODS", "3")
    uploader = "Repeated Junk"
    query = "mlbb ranked"
    for index in range(3):
        vid = f"junkvideo{index}"
        meta = _meta(vid, uploader, query)
        record_vod_outcome(meta, sent=0)
        if index < 2:
            record_owner_feedback(vid, label="bad", item_id=f"bad-{index}")
    assert uploader_hard_blocked(_meta("candidate01", uploader, query)) is True
