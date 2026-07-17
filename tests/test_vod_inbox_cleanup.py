"""Tests for exhausted VOD inbox cleanup."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import vod_inbox_cleanup as cleanup  # noqa: E402


def _pubg_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "pubg"
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    return root


def test_is_fully_spent_requires_exhausted() -> None:
    assert cleanup.is_fully_spent(None) is False
    assert cleanup.is_fully_spent({}) is False
    assert cleanup.is_fully_spent({"exhausted": False}) is False
    assert cleanup.is_fully_spent({"exhausted": True, "reject_reason": "all_peaks_blocked"}) is True


def test_cleanup_deletes_exhausted_file(tmp_path, monkeypatch) -> None:
    root = _pubg_root(tmp_path, monkeypatch)
    monkeypatch.setenv("VOD_INBOX_DELETE_EXHAUSTED", "1")
    monkeypatch.setenv("VOD_INBOX_DELETE_GRACE_SEC", "0")
    monkeypatch.setenv("VOD_INBOX_DELETE_ALL_EXHAUSTED", "1")

    inbox = root / "youtube_nightly" / "inbox"
    vod = inbox / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"x" * 2048)

    state = {
        "vods": [
            {
                "id": "abcdefghijk",
                "path": str(vod),
                "exhausted": True,
                "reject_reason": "all_peaks_blocked",
                "exhausted_at": time.time() - 10,
            }
        ],
        "used_youtube_ids": ["abcdefghijk"],
    }
    (root / "vod_segment_state.json").write_text(json.dumps(state), encoding="utf-8")

    result = cleanup.cleanup_game("pubg", limit=10)
    assert result["deleted"] == 1
    assert not vod.exists()
    saved = json.loads((root / "vod_segment_state.json").read_text(encoding="utf-8"))
    row = saved["vods"][0]
    assert row["file_deleted"] is True
    assert row["path"] == ""
    assert "abcdefghijk" in saved["used_youtube_ids"]


def test_cleanup_skips_active_vod(tmp_path, monkeypatch) -> None:
    root = _pubg_root(tmp_path, monkeypatch)
    monkeypatch.setenv("VOD_INBOX_DELETE_EXHAUSTED", "1")
    monkeypatch.setenv("VOD_INBOX_DELETE_GRACE_SEC", "0")

    inbox = root / "youtube_nightly" / "inbox"
    vod = inbox / "yt_activevod01.mp4"
    vod.write_bytes(b"active")

    state = {
        "vods": [
            {
                "id": "activevod01",
                "path": str(vod),
                "exhausted": False,
            }
        ],
        "used_youtube_ids": [],
    }
    (root / "vod_segment_state.json").write_text(json.dumps(state), encoding="utf-8")

    result = cleanup.cleanup_game("pubg", limit=10)
    assert result["deleted"] == 0
    assert vod.exists()


def test_grace_defers_delete(tmp_path, monkeypatch) -> None:
    root = _pubg_root(tmp_path, monkeypatch)
    monkeypatch.setenv("VOD_INBOX_DELETE_EXHAUSTED", "1")
    monkeypatch.setenv("VOD_INBOX_DELETE_GRACE_SEC", "3600")
    monkeypatch.setenv("VOD_INBOX_DELETE_ALL_EXHAUSTED", "1")

    inbox = root / "youtube_nightly" / "inbox"
    vod = inbox / "yt_gracevod001.mp4"
    vod.write_bytes(b"grace")

    entry = {
        "id": "gracevod001",
        "path": str(vod),
        "exhausted": True,
        "reject_reason": "no_combat_peaks",
        "exhausted_at": time.time(),
    }
    result = cleanup.delete_exhausted_file(entry)
    assert result["deleted"] is False
    assert result["reason"] == "grace"
    assert vod.exists()

    deferred = cleanup.cleanup_after_exhaust("pubg", entry, state={"vods": [entry]})
    assert deferred["reason"] == "deferred_grace"
    assert vod.exists()
