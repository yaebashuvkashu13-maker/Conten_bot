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


def test_used_ids_allow_retryable_exhausted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(tmp_path / "pubg"))
    state = {
        "used_youtube_ids": ["ZF3LCQ3080M", "BADTITLE000"],
        "vods": [
            {
                "id": "ZF3LCQ3080M",
                "path": "",
                "exhausted": True,
                "reject_reason": "fast_probe_too_short",
            },
            {
                "id": "BADTITLE000",
                "path": "",
                "exhausted": True,
                "reject_reason": "bad_title",
            },
            {
                "id": "QDda58YJxUY",
                "path": "",
                "exhausted": True,
                "reject_reason": "no_combat_peaks",
            },
        ],
    }

    class _Fake:
        @staticmethod
        def load_state(_g):
            return state

    monkeypatch.setitem(sys.modules, "vod_game_registry", _Fake)
    used = pool.used_ids_for_game("pubg")
    assert "ZF3LCQ3080M" not in used
    assert "QDda58YJxUY" not in used
    assert "BADTITLE000" in used


def test_used_ids_blocks_spent_peaks_and_duplicate_rows(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(tmp_path / "pubg"))
    state = {
        "used_youtube_ids": [],
        "vods": [
            {
                "id": "QDda58YJxUY",
                "path": "",
                "exhausted": True,
                "reject_reason": "no_combat_peaks",
                "last_pool_peaks": [{"peak_sec": 286.0}],
                "last_scan_blocked": True,
                "file_deleted": True,
            },
            {
                "id": "QDda58YJxUY",
                "path": "",
                "exhausted": False,
            },
            {
                "id": "BlockedPk01",
                "path": "",
                "exhausted": True,
                "reject_reason": "all_peaks_blocked",
            },
        ],
    }

    class _Fake:
        @staticmethod
        def load_state(_g):
            return state

    monkeypatch.setitem(sys.modules, "vod_game_registry", _Fake)
    used = pool.used_ids_for_game("pubg")
    assert "QDda58YJxUY" in used
    assert "BlockedPk01" in used


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
