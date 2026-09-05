"""Tests for unified production hardening: bypass defaults, feedback bridge."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_hang_recover_never_enables_bypass() -> None:
    from vod_hang_detector import apply_agent_recover_env

    out = apply_agent_recover_env({}, escalation=2)
    assert out["VOD_FORCE_PRESEND_BYPASS"] == "0"
    assert out["VOD_FORCE_SKIP_DISCOVERY"] == "0"
    assert out["SHOOTER_VOD_SKIP_DISCOVERY"] == "0"
    assert out.get("PUBG_PRESEND_SHOOTING_GATE", "1") != "0"


def test_force_send_source_keeps_bypass_off_and_gate_on() -> None:
    import vod_force_send as vfs

    src = Path(vfs.__file__).read_text(encoding="utf-8")
    assert 'env["VOD_FORCE_PRESEND_BYPASS"] = "0"' in src
    assert 'env["SHOOTER_VOD_SKIP_DISCOVERY"] = "0"' in src
    assert 'VOD_FORCE_PRESEND_GATE", "1"' in src
    assert 'VOD_FORCE_PRESEND_GATE", "0"' not in src


def test_owner_feedback_bridge_tightens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_QUALITY_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("VOD_ADAPTIVE_THRESH_DIR", str(tmp_path / "adaptive"))
    from game_adaptive_thresholds import thresholds_for
    from vod_owner_feedback_bridge import apply_owner_feedback

    before = thresholds_for("pubg")
    out = apply_owner_feedback(
        "pubg",
        clip_id="c1",
        is_good=False,
        reason="loot_run",
        vod_id="v1",
    )
    assert out["ledger"] is True
    after = thresholds_for("pubg")
    assert after["gun_density_min"] >= before["gun_density_min"]


def test_production_feed_is_full_tree_not_slim() -> None:
    feed = (SCRIPTS / "shooter_vod_segment_feed.py").read_text(encoding="utf-8")
    assert feed.count("\n") > 2500
    assert "hook_gate_clip" in feed
    assert 'setdefault("VOD_FORCE_PRESEND_BYPASS", "0")' in feed
    assert "rank_peaks_cheap" in feed
    assert "audio_preflight_ok" in feed
    assert "apply_to_environ" in feed
    assert 'return True, "keepalive_esc2_pass"' not in feed


def test_inbox_skips_hard_bad_without_peaks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vod_inbox_recover as vir

    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    parked = root / "youtube_nightly" / "parked"
    inbox.mkdir(parents=True)
    parked.mkdir(parents=True)
    bad = parked / "yt_BADMENU0001.mp4"
    bad.write_bytes(b"x" * 50_000_000)
    good = parked / "yt_GOODFIGHT001.mp4"
    good.write_bytes(b"y" * 50_000_000)
    state = root / "vod_segment_state.json"
    state.write_text(
        json.dumps(
            {
                "vods": [
                    {
                        "id": "BADMENU0001",
                        "exhausted": True,
                        "reject_reason": "hard_loot_walk",
                        "peaks": [],
                    },
                    {
                        "id": "GOODFIGHT001",
                        "exhausted": True,
                        "reject_reason": "no_sendable_peaks",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(vir, "_game_roots", lambda game="pubg": (inbox, parked, state))
    moved = vir.unpark_recent("pubg", limit=5, min_bytes=1_000_000)
    assert "yt_BADMENU0001.mp4" not in moved
    assert "yt_GOODFIGHT001.mp4" in moved
    assert not (inbox / "yt_BADMENU0001.mp4").exists()
    assert (inbox / "yt_GOODFIGHT001.mp4").exists()


def test_reject_reason_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_QUALITY_LEDGER_DIR", str(tmp_path / "ledger"))
    from vod_clip_quality_ledger import record_decision, record_send, reject_reason_summary

    record_decision(
        "pubg",
        clip_id="r1",
        vod_id="v1",
        decision="reject",
        reason="early_payoff_low=0.05",
    )
    record_decision(
        "pubg",
        clip_id="r2",
        vod_id="v1",
        decision="reject",
        reason="payoff_low=0.08",
    )
    record_send(
        "pubg",
        clip_id="s1",
        vod_id="v1",
        rendered_path="/tmp/x.mp4",
        metrics={"singles_gun_payoff_bypass": True},
    )
    summary = reject_reason_summary("pubg")
    assert summary["rejected"] == 2
    assert summary["sent"] == 1
    assert summary["gun_bypass_admits"] >= 1
    assert summary["early_payoff_low"] >= 1
