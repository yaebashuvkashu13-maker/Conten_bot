"""Tests for unified production hardening: bypass defaults, feedback bridge."""

from __future__ import annotations

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
    assert out.get("PUBG_PRESEND_SHOOTING_GATE", "1") != "0"


def test_force_send_source_keeps_bypass_off() -> None:
    import vod_force_send as vfs

    src = Path(vfs.__file__).read_text(encoding="utf-8")
    assert 'env["VOD_FORCE_PRESEND_BYPASS"] = "0"' in src
    assert 'env["SHOOTER_VOD_SKIP_DISCOVERY"] = "0"' in src


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
    assert 'VOD_FORCE_PRESEND_BYPASS", "0"' in feed
    assert "rank_peaks_cheap" in feed
    assert "audio_preflight_ok" in feed
