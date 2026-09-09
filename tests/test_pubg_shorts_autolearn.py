#!/usr/bin/env python3
"""Unit tests for PUBG Shorts autolearn feature table + report cadence."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_quantile_schedule_tightens() -> None:
    from pubg_shorts_feature_table import quantile_schedule

    assert quantile_schedule(10)[2] == "warm_wide"
    assert quantile_schedule(150)[2] == "forming"
    assert quantile_schedule(400)[2] == "tightening"
    assert quantile_schedule(800)[2] == "minimal"
    assert quantile_schedule(10)[0] < quantile_schedule(800)[0]


def test_feature_table_append_and_ranges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_SHORTS_AUTOLEARN_DIR", str(tmp_path / "al"))
    monkeypatch.setenv("PUBG_SHORTS_RANGE_MIN", "10")
    from pubg_shorts_feature_table import (
        append_feature_row,
        build_silver_ranges,
        features_csv_path,
        format_ranges_table,
        iter_feature_rows,
        row_from_short_metrics,
    )

    for i in range(30):
        report = {
            "gunfire_density": 0.05 + i * 0.001,
            "burst_ratio": 5.0 + i * 0.1,
            "audio_rms": 0.03,
            "center_motion": 0.08,
            "center_text": 0.04,
            "panns_gunshot": 0.3,
            "panns_machine_gun": 0.4,
            "panns_gun_max": 0.4,
            "panns_speech": 0.1,
            "panns_music": 0.05,
            "killfeed_density": 0.2,
            "quality_score": 0.4,
            "heuristic_score": 0.35,
            "ranker_score": 0.45,
            "duration": 14.0,
            "has_author_kill": True,
            "loot_walk": False,
        }
        row = row_from_short_metrics(
            video_id=f"abcdefghij{i%10}",
            title=f"metro royale fight {i}",
            source_url="https://youtube.com/shorts/x",
            search_query="metro",
            path=f"/tmp/x{i}.mp4",
            meta={"duration": 14, "view_count": 1000 + i},
            quality_report=report,
            quality_ok=True,
            reject_reason="",
            batch_id=i,
        )
        append_feature_row(row)

    rows = iter_feature_rows()
    assert len(rows) == 30
    assert features_csv_path().is_file()
    blob = build_silver_ranges()
    assert blob["stage"] == "warm_wide"
    assert blob["ready"] is True
    assert "gunfire_density" in blob["ranges"]
    text = format_ranges_table(blob)
    assert "gunfire_density" in text


def test_batch_report_every_100(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_SHORTS_AUTOLEARN_DIR", str(tmp_path / "al"))
    monkeypatch.setenv("PUBG_SHORTS_REPORT_EVERY", "100")
    from pubg_shorts_autolearn import maybe_send_batch_report
    from pubg_shorts_feature_table import save_state

    sent: list[str] = []

    def fake_send(text: str, **_kwargs) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr("pubg_shorts_autolearn.send_message", fake_send)
    state = {
        "watched_total": 100,
        "last_report_at_count": 0,
    }
    save_state(state)
    ok = maybe_send_batch_report(
        state,
        {
            "stage": "forming",
            "low_q": 0.1,
            "high_q": 0.9,
            "rows_used": 80,
            "rows_total": 100,
            "ready": True,
            "ranges": {"gunfire_density": {"min": 0.04, "max": 0.09, "p50": 0.06, "n": 80}},
        },
    )
    assert ok is True
    assert state["last_report_at_count"] == 100
    assert "Просмотрено 100" in sent[0]
    assert "gunfire_density" in sent[0]

    # Not due again until next milestone (200), even if +few clips.
    state["watched_total"] = 105
    save_state(state)
    ok2 = maybe_send_batch_report(state, {"ranges": {}})
    assert ok2 is False

    state["watched_total"] = 199
    save_state(state)
    assert maybe_send_batch_report(state, {"ranges": {}}) is False

    state["watched_total"] = 200
    save_state(state)
    blob2 = {
        "stage": "forming",
        "low_q": 0.1,
        "high_q": 0.9,
        "rows_used": 180,
        "rows_total": 200,
        "ready": True,
        "ranges": {"gunfire_density": {"min": 0.1, "max": 0.2, "p50": 0.15, "n": 1}},
    }
    assert maybe_send_batch_report(state, blob2) is True
    assert state["last_report_at_count"] == 200
    assert len(sent) == 2
    assert "Следующий отчёт на 300" in sent[1]

def test_pubg_queries_merged_and_large(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PUBG_SHORTS_AUTOLEARN_QUERIES", raising=False)
    # Point calibration YAML at repo config via default load_games path.
    from pubg_shorts_autolearn import DEFAULT_QUERIES, HIGHLIGHT_QUERIES, _queries

    shorts_q = _queries(mode="shorts")
    assert len(shorts_q) >= 20
    assert len(set(q.lower() for q in shorts_q)) == len(shorts_q)
    assert any("метро" in q.lower() or "metro" in q.lower() for q in shorts_q)
    hl = _queries(mode="highlights")
    assert len(hl) >= len(HIGHLIGHT_QUERIES) - 1
    assert len(DEFAULT_QUERIES) >= 20


def test_title_gate_blocks_meme() -> None:
    from pubg_shorts_autolearn import _title_ok

    assert _title_ok("метро роял перестрелка пабг") is True
    assert _title_ok("funny meme metro royale") is False


def test_find_inbox_vod_prefers_pubg(tmp_path: Path) -> None:
    from pubg_shorts_autolearn import find_inbox_vod

    mlbb = tmp_path / "mlbb" / "inbox"
    pubg = tmp_path / "pubg" / "youtube_nightly" / "inbox"
    mlbb.mkdir(parents=True)
    pubg.mkdir(parents=True)
    (mlbb / "yt_oldmlbbxxxx.mp4").write_bytes(b"x" * 6_000_000)
    target = pubg / "yt_newpubgxxxx.mp4"
    target.write_bytes(b"y" * 6_000_000)
    assert find_inbox_vod(roots=[mlbb, pubg]) == target


def test_local_pool_lists_mp4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_SHORTS_AUTOLEARN_DIR", str(tmp_path / "al"))
    pool = tmp_path / "pool"
    pool.mkdir()
    (pool / "yt_abcdefghijk.mp4").write_bytes(b"x" * 50_000)
    monkeypatch.setenv("PUBG_SHORTS_LOCAL_POOLS", f"testpool:{pool}")
    from pubg_shorts_autolearn import list_local_candidates

    hits = list_local_candidates(seen=set(), limit=10)
    assert len(hits) == 1
    assert hits[0]["video_id"] == "abcdefghijk"
    assert hits[0]["source"] == "testpool"


def test_hourly_and_daily_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_SHORTS_MAX_DL_PER_HOUR", "17")
    monkeypatch.setenv("PUBG_SHORTS_MAX_DL_PER_DAY", "385")
    from pubg_shorts_autolearn import _rate_bump, _rate_ok

    state: dict = {}
    assert _rate_ok(state, kind="download") is True
    state["download_hour_count"] = 17
    state["download_hour_ts"] = __import__("time").time()
    assert _rate_ok(state, kind="download") is False

    state = {"download_day_count": 385, "download_day_ts": __import__("time").time()}
    assert _rate_ok(state, kind="download") is False

    state = {}
    _rate_bump(state, kind="download")
    assert state["download_hour_count"] == 1
    assert state["download_day_count"] == 1


def test_search_miss_budget_separate_from_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_SHORTS_MAX_SEARCH_PER_HOUR", "2")
    monkeypatch.setenv("PUBG_SHORTS_MAX_SEARCH_MISS_PER_HOUR", "5")
    from pubg_shorts_autolearn import _rate_bump, _rate_ok

    state: dict = {"search_hour_count": 2, "search_hour_ts": __import__("time").time()}
    assert _rate_ok(state, kind="search") is False
    assert _rate_ok(state, kind="search_miss") is True
    _rate_bump(state, kind="search_miss")
    assert state["search_miss_hour_count"] == 1


def test_record_watched_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_SHORTS_AUTOLEARN_DIR", str(tmp_path / "al"))
    from pubg_shorts_feature_table import load_state, record_watched, save_state

    a = {"watched_total": 10, "seen_ids": ["old"], "batches": 10}
    save_state(a)
    b = load_state()
    assert record_watched(b, seen_keys=["new1"], counter_keys=["local_watched"]) is True
    assert b["watched_total"] == 11
    # Second writer with stale base still increments on disk.
    stale = {"watched_total": 10, "seen_ids": ["old"], "batches": 10}
    assert record_watched(stale, seen_keys=["new2"], counter_keys=["shorts_watched"]) is True
    final = load_state()
    assert final["watched_total"] == 12
    assert "new1" in final["seen_ids"] and "new2" in final["seen_ids"]
    assert final["local_watched"] == 1
    assert final["shorts_watched"] == 1


def test_progress_ping_every_25(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_SHORTS_AUTOLEARN_DIR", str(tmp_path / "al"))
    monkeypatch.setenv("PUBG_SHORTS_PROGRESS_EVERY", "25")
    monkeypatch.setenv("PUBG_SHORTS_REPORT_EVERY", "100")
    from pubg_shorts_autolearn import maybe_send_progress_ping
    from pubg_shorts_feature_table import save_state

    sent: list[str] = []
    monkeypatch.setattr(
        "pubg_shorts_autolearn.send_message",
        lambda text, **_k: sent.append(text) or True,
    )
    state = {"watched_total": 50, "last_progress_at_count": 25}
    save_state(state)
    assert maybe_send_progress_ping(state) is True
    assert "просмотрено 50" in sent[0]
    assert state["last_progress_at_count"] == 50
    assert maybe_send_progress_ping(state) is False
