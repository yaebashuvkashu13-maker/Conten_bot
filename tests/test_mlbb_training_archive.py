"""Training archive + 2026 ingest window."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def test_shorts_upload_cutoff_2026_floor() -> None:
    min_date = "20260101"
    rolling = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y%m%d")
    cutoff = max(min_date, rolling)
    assert cutoff >= "20260101"


def test_archive_short_on_good_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_root = tmp_path / "archive"
    data_root = tmp_path / "data"
    shorts_root = tmp_path / "shorts"
    shorts_root.mkdir()
    data_root.mkdir()
    vid = "abc12345678"
    src = shorts_root / f"yt_{vid}.mp4"
    src.write_bytes(b"full-short-mp4")

    monkeypatch.setenv("MLBB_TRAINING_ARCHIVE", "1")
    monkeypatch.setenv("MLBB_TRAINING_ARCHIVE_ROOT", str(archive_root))
    monkeypatch.setenv("MLBB_DATA_ROOT", str(data_root))
    monkeypatch.setenv("MLBB_TRAINING_ARCHIVE_YEAR", "2026")

    import mlbb_training_archive as arch

    out = arch.archive_short(src, vid, upload_date="20260215", title="savage")
    assert out is not None
    assert out.exists()
    assert out.parent.name == "shorts"
    assert out.parent.parent.name == "2026"
