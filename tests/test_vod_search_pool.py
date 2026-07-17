"""Tests for per-game YouTube VOD search pools."""

from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import vod_search_pool as pool  # noqa: E402


def test_merge_candidates_dedupes_and_respects_used(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path / "mlbb"))
    existing = [{"id": "aaaaaaaaaaa", "title": "old"}, {"id": "bbbbbbbbbbb", "title": "keep"}]
    fresh = [{"id": "ccccccccccc", "title": "new"}, {"id": "aaaaaaaaaaa", "title": "dup"}]
    used = {"bbbbbbbbbbb"}
    merged = pool._merge_candidates(existing, fresh, used=used, max_size=10)
    ids = [r["id"] for r in merged]
    assert ids[0] == "ccccccccccc"
    assert "aaaaaaaaaaa" in ids
    assert "bbbbbbbbbbb" not in ids


def test_pop_candidate_removes_from_pool(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path / "mlbb"))
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(tmp_path / "pubg"))
    payload = {
        "candidates": [
            {"id": "11111111111", "title": "a"},
            {"id": "22222222222", "title": "b"},
        ],
        "last_refresh_at": time.time(),
    }
    pool.save_pool("pubg", payload)
    pick = pool.pop_candidate("pubg", used=set())
    assert pick is not None
    assert pick["id"] == "11111111111"
    left = pool.load_pool("pubg")["candidates"]
    assert [r["id"] for r in left] == ["22222222222"]


def test_pool_needs_refresh_respects_gap(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path / "mlbb"))
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(tmp_path / "pubg"))
    monkeypatch.setenv("VOD_SEARCH_POOL_REFRESH_GAP_SEC", "600")
    monkeypatch.setenv("VOD_SEARCH_POOL_MIN", "4")
    pool.save_pool(
        "pubg",
        {"candidates": [], "last_refresh_at": time.time()},
    )
    assert pool.pool_needs_refresh("pubg", used=set()) is False


def test_acquire_release_search_slot(monkeypatch) -> None:
    monkeypatch.setenv("VOD_SEARCH_MAX_CONCURRENT", "1")
    monkeypatch.setenv("VOD_SEARCH_MIN_INTERVAL_SEC", "0")
    # Reset module globals for isolation.
    with pool._LIMITER_LOCK:
        pool._SEARCH_INFLIGHT = 0
        pool._LAST_SEARCH_AT = 0.0
    assert pool.acquire_search_slot(timeout=1) is True
    assert pool.acquire_search_slot(timeout=0.2) is False
    pool.release_search_slot()
    assert pool.acquire_search_slot(timeout=1) is True
    pool.release_search_slot()
