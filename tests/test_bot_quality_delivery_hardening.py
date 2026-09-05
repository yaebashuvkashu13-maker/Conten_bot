"""Tests for quality ledger, adaptive thresholds, media cache, delivery, worker guard."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_quality_ledger_send_and_feedback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_QUALITY_LEDGER_DIR", str(tmp_path / "ledger"))
    from vod_clip_quality_ledger import feedback_stats_for_vod, record_feedback, record_send

    record_send(
        "pubg",
        clip_id="clip1",
        vod_id="ytABC",
        rendered_path="/tmp/x.mp4",
        metrics={"vod_path": "/data/ytABC.mp4", "gun_density": 0.09},
        admit_reason="shooter_combat_ok",
        peak_sec=12.0,
    )
    record_feedback("pubg", clip_id="clip1", label="bad", reason="loot_run", vod_id="ytABC")
    stats = feedback_stats_for_vod("pubg", "ytABC")
    assert stats["sent"] == 1
    assert stats["bad"] == 1
    assert stats["good"] == 0


def test_adaptive_thresholds_tighten_on_run_menu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_ADAPTIVE_THRESH_DIR", str(tmp_path / "thresh"))
    import game_adaptive_thresholds as gat

    # Do not leak SMART_* env into other gate tests in the same pytest process.
    monkeypatch.setattr(gat, "apply_to_environ", lambda game: gat.thresholds_for(game))

    base = gat.thresholds_for("pubg")
    after_other = gat.note_negative_feedback("pubg", "boring")
    assert after_other["gun_density_min"] == base["gun_density_min"]
    after_run = gat.note_negative_feedback("pubg", "loot_run")
    assert after_run["gun_density_min"] > base["gun_density_min"]
    assert after_run["burst_ratio_min"] > base["burst_ratio_min"]
    assert after_run["motion_max_run"] < base["motion_max_run"]
    after_menu = gat.note_negative_feedback("standoff", "menu_lobby")
    assert after_menu["gun_density_min"] >= gat.thresholds_for("standoff")["gun_density_min"] - 1e-9


def test_media_cache_ffprobe_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_MEDIA_CACHE_DIR", str(tmp_path / "cache"))
    from vod_media_cache import audio_preflight_ok, cached_ffprobe

    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"fake")
    calls = {"n": 0}

    def probe(_p: Path) -> dict:
        calls["n"] += 1
        return {
            "format": {"duration": "120.0"},
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
        }

    a = cached_ffprobe(vod, probe)
    b = cached_ffprobe(vod, probe)
    assert a["format"]["duration"] == "120.0"
    assert b["format"]["duration"] == "120.0"
    assert calls["n"] == 1
    ok, reason, meta = audio_preflight_ok(vod, probe_fn=probe)
    assert ok and reason == "ok"
    assert meta["duration"] == 120.0


def test_telegram_already_ready_skips_reencode(tmp_path: Path) -> None:
    from telegram_delivery import already_telegram_ready, encode_telegram_mp4

    src = tmp_path / "clip.mp4"
    src.write_bytes(b"x" * 1000)
    fake_meta = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {"codec_type": "audio", "codec_name": "aac"},
        ]
    }
    with patch("telegram_delivery.subprocess.check_output", return_value=json.dumps(fake_meta)):
        assert already_telegram_ready(src) is True
    with patch("telegram_delivery.subprocess.check_output", return_value=json.dumps(fake_meta)):
        with patch("telegram_delivery.subprocess.run") as run:
            out = tmp_path / "out.mp4"

            def _run(cmd, **kwargs):
                out.write_bytes(b"y" * 100)
                return MagicMock(returncode=0)

            run.side_effect = _run
            result = encode_telegram_mp4(src, out)
            assert result == out or result == src


def test_worker_guard_quarantine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_WORKER_GUARD_DIR", str(tmp_path / "guard"))
    monkeypatch.setenv("VOD_QUARANTINE_FAILS", "2")
    monkeypatch.setenv("VOD_QUARANTINE_HOURS", "1")
    from vod_worker_guard import heartbeat_stale, is_quarantined, note_vod_failure, write_heartbeat

    write_heartbeat("pubg_feed", phase="test")
    assert heartbeat_stale("pubg_feed", max_age_sec=3600) is False
    note_vod_failure("vod1", reason="timeout")
    assert is_quarantined("vod1") is False
    note_vod_failure("vod1", reason="timeout")
    assert is_quarantined("vod1") is True


def test_spotcheck_rejects_tiny(tmp_path: Path) -> None:
    from encoded_clip_spotcheck import spotcheck_encoded_clip

    p = tmp_path / "tiny.mp4"
    p.write_bytes(b"abc")
    ok, reason, _ = spotcheck_encoded_clip(p)
    assert ok is False
    assert "tiny" in reason or "missing" in reason
