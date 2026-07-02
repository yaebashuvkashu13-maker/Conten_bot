"""Inbox backlog blocks discovery; partial VODs should not clog the queue."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from shooter_vod_inbox import (  # noqa: E402
    discovery_blocked,
    pending_inbox_work,
    retryable_reject_reason,
)


def test_retryable_reasons() -> None:
    assert retryable_reject_reason("score_timeout:2") is True
    assert retryable_reject_reason("fast_panns_0/1 top=0.08") is True
    assert retryable_reject_reason("partial_download=45s") is False


def test_discovery_blocked_when_inbox_full(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_DISCOVERY_MAX_INBOX", "3")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    registry: list[dict] = []
    for i in range(4):
        p = inbox / f"yt_vid{i}.mp4"
        p.write_bytes(b"x" * 1000)

    def dur(_p: Path) -> float:
        return 300.0

    assert discovery_blocked(inbox, registry, min_sec=180.0, duration_fn=dur, vod_id_fn=lambda p: p.stem[3:]) is True
    assert pending_inbox_work(inbox, registry, min_sec=180.0, duration_fn=dur, vod_id_fn=lambda p: p.stem[3:]) == 4


def test_pending_counts_retryable_exhausted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_DISCOVERY_MAX_INBOX", "20")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    p = inbox / "yt_abc.mp4"
    p.write_bytes(b"x" * 1000)
    registry = [{"id": "abc", "path": str(p), "exhausted": True, "reject_reason": "score_timeout:1"}]

    def dur(_p: Path) -> float:
        return 240.0

    assert pending_inbox_work(inbox, registry, min_sec=180.0, duration_fn=dur, vod_id_fn=lambda p: p.stem[3:]) == 1
    assert discovery_blocked(inbox, registry, min_sec=180.0, duration_fn=dur, vod_id_fn=lambda p: p.stem[3:]) is False
