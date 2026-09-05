"""Tests for weekly report, dislike-reason gates, hook gate, cheap cascade, upload retry."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_weekly_report_from_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_QUALITY_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("VOD_QUALITY_REPORT_DIR", str(tmp_path / "reports"))
    from vod_clip_quality_ledger import record_feedback, record_send
    from vod_weekly_quality_report import build_weekly_report, format_report_text, write_report

    record_send(
        "pubg",
        clip_id="c1",
        vod_id="v1",
        rendered_path="/tmp/a.mp4",
        metrics={"gun_density": 0.11, "panns_gun": 0.4, "killfeed": 0.2},
        admit_reason="ok",
        peak_sec=10,
    )
    record_feedback("pubg", clip_id="c1", label="bad", reason="loot_run", vod_id="v1")
    record_feedback("pubg", clip_id="c1", label="good", reason="", vod_id="v1")
    report = build_weekly_report(["pubg"], days=7)
    block = report["games"]["pubg"]
    assert block["sent"] == 1
    assert block["feedback_bad"] == 1
    assert block["feedback_good"] == 1
    assert block["top_dislike_reasons"][0][0] == "loot_run"
    text = format_report_text(report)
    assert "PUBG" in text and "loot_run" in text
    jp, tp = write_report(report, out_dir=tmp_path / "reports")
    assert jp.exists() and tp.exists()


def test_dislike_reason_gates_menu_and_loot() -> None:
    from dislike_reason_gates import evaluate_reason_gates, normalize_reason

    assert normalize_reason("menu_lobby") == "menu"
    assert normalize_reason("беготня") == "loot_run"
    ok, reason, _ = evaluate_reason_gates(
        {"gun_density": 0.02, "burst_ratio": 2.0, "center_motion": 0.25, "menu_overlay": 0.05},
        active_reasons=["loot_run"],
    )
    assert ok is False
    assert "gun" in reason or "loot" in reason or "burst" in reason
    ok2, _, _ = evaluate_reason_gates(
        {
            "gun_density": 0.20,
            "burst_ratio": 9.0,
            "center_motion": 0.05,
            "menu_overlay": 0.05,
            "visual": 0.8,
        },
        active_reasons=["loot_run", "menu"],
    )
    assert ok2 is True


def test_hook_gate_rejects_missing(tmp_path: Path) -> None:
    from clip_hook_gate import hook_gate_clip

    missing = tmp_path / "nope.mp4"
    ok, reason, _ = hook_gate_clip(missing)
    assert ok is False
    assert "missing" in reason


def test_cheap_cascade_ranks_by_gun(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_MEDIA_CACHE_DIR", str(tmp_path / "cache"))
    from vod_cheap_cascade import rank_peaks_cheap, should_run_heavy

    vod = tmp_path / "vod.mp4"
    vod.write_bytes(b"fake-vod")

    def audio_fn(_p, start, _dur):
        gun = 0.4 if start >= 90 else 0.02
        return {"rms": gun, "gun_proxy": gun}

    ranked = rank_peaks_cheap(vod, [10.0, 100.0, 40.0], top_k=2, min_gun=0.05, audio_fn=audio_fn)
    assert ranked
    assert ranked[0]["peak_sec"] == 100.0
    assert should_run_heavy(ranked[0], rank=0) is True


def test_upload_retry_only_network(monkeypatch: pytest.MonkeyPatch) -> None:
    from telegram_delivery import TelegramUploadQueue, is_retryable_upload_error
    import telegram_delivery as td

    monkeypatch.setattr(td.time, "sleep", lambda _s: None)

    assert is_retryable_upload_error(TimeoutError("timed out"))
    assert is_retryable_upload_error(RuntimeError("HTTP 429 flood"))
    assert is_retryable_upload_error(ValueError("invalid chat id")) is False

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("connection reset by peer")
        return "ok"

    q = TelegramUploadQueue(workers=1, max_attempts=4)
    assert q.submit(flaky).result(timeout=5) == "ok"
    assert calls["n"] == 3

    def hard_fail():
        raise ValueError("bad request")

    q2 = TelegramUploadQueue(workers=1, max_attempts=4)
    with pytest.raises(ValueError):
        q2.submit(hard_fail).result(timeout=5)


def test_media_cache_ocr_motion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_MEDIA_CACHE_DIR", str(tmp_path / "cache"))
    from vod_media_cache import cached_motion_window, cached_ocr_window

    vod = tmp_path / "x.mp4"
    vod.write_bytes(b"abc")
    calls = {"ocr": 0, "motion": 0}

    def ocr(_p, _s, _d):
        calls["ocr"] += 1
        return {"text": "KILL"}

    def motion(_p, _s, _d):
        calls["motion"] += 1
        return {"motion": 0.4}

    a = cached_ocr_window(vod, 1.0, 2.0, ocr)
    b = cached_ocr_window(vod, 1.0, 2.0, ocr)
    c = cached_motion_window(vod, 1.0, 2.0, motion)
    d = cached_motion_window(vod, 1.0, 2.0, motion)
    assert a["text"] == "KILL" and b["text"] == "KILL"
    assert c["motion"] == 0.4 and d["motion"] == 0.4
    assert calls["ocr"] == 1 and calls["motion"] == 1


def test_adaptive_reason_families(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_ADAPTIVE_THRESH_DIR", str(tmp_path / "th"))
    import game_adaptive_thresholds as gat

    monkeypatch.setattr(gat, "apply_to_environ", lambda game: gat.thresholds_for(game))
    base = gat.thresholds_for("pubg")
    after_gun = gat.note_negative_feedback("pubg", "no_gun")
    assert after_gun["gun_density_min"] > base["gun_density_min"]
    after_render = gat.note_negative_feedback("pubg", "bad_render")
    assert after_render["menu_overlay_max"] <= after_gun["menu_overlay_max"]


def test_inbox_recover_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vod_inbox_recover as vir

    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    parked = root / "youtube_nightly" / "parked"
    inbox.mkdir(parents=True)
    parked.mkdir(parents=True)
    big = parked / "yt_ABCDEFGHIJK.mp4"
    big.write_bytes(b"x" * 50_000_000)
    stub = inbox / "yt_LIVESTUB1234.mp4"
    stub.write_bytes(b"tiny")
    state = root / "vod_segment_state.json"
    state.write_text(
        json.dumps(
            {
                "vods": [
                    {
                        "id": "ABCDEFGHIJK",
                        "exhausted": True,
                        "reject_reason": "no_sendable_peaks",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(vir, "_game_roots", lambda game="pubg": (inbox, parked, state))
    removed = vir.drop_live_stubs("pubg", max_bytes=1000)
    moved = vir.unpark_recent("pubg", limit=2, min_bytes=1_000_000)
    cleared = vir.clear_exhausted("pubg", moved)
    assert "yt_LIVESTUB1234.mp4" in removed
    assert "yt_ABCDEFGHIJK.mp4" in moved
    assert (inbox / "yt_ABCDEFGHIJK.mp4").exists()
    assert cleared >= 1
