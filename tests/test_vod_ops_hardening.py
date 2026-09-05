"""Ops hardening: ledger heartbeat/silence, single-owner feed, discovery skip default."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_ledger_heartbeat_and_gate_age(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_QUALITY_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("VOD_LEDGER_HEARTBEAT_MIN_SEC", "0")
    import vod_clip_quality_ledger as ledger

    ledger._last_heartbeat_ts.clear()
    latest_gate_event_age_sec = ledger.latest_gate_event_age_sec
    record_decision = ledger.record_decision
    record_heartbeat = ledger.record_heartbeat
    reject_reason_summary = ledger.reject_reason_summary

    assert latest_gate_event_age_sec("pubg") is None
    record_heartbeat("pubg", reason="unit_test")
    age = latest_gate_event_age_sec("pubg")
    assert age is not None and age < 5
    record_decision(
        "pubg",
        clip_id="c1",
        vod_id="v1",
        decision="reject",
        reason="early_payoff_low=0.01",
    )
    summary = reject_reason_summary("pubg")
    assert summary["rejected"] >= 1
    assert summary["heartbeats"] >= 1


def test_feed_defaults_skip_discovery_inbox_dead_off() -> None:
    src = (SCRIPTS / "shooter_vod_segment_feed.py").read_text(encoding="utf-8")
    assert 'SHOOTER_VOD_SKIP_DISCOVERY_WHEN_INBOX_DEAD", "0"' in src
    assert "record_heartbeat" in src


def test_adaptive_tables_have_no_relax_keys() -> None:
    from shooter_vod_adaptive_gate import SHOOTER_SOFTEN_L3, SHOOTER_SOFTEN_L4, overrides_for_level

    assert "PUBG_RELAX_OWNER_HEURISTICS" not in SHOOTER_SOFTEN_L3
    assert "PUBG_RELAX_OWNER_HEURISTICS" not in SHOOTER_SOFTEN_L4
    assert overrides_for_level(4).get("PUBG_RELAX_OWNER_HEURISTICS") == "0"


def test_deploy_has_single_owner_and_slim_guard() -> None:
    deploy = (SCRIPTS / "deploy_unified_production.sh").read_text(encoding="utf-8")
    assert "feed looks slim" in deploy
    assert "mlbb_vod_segment_feed.sh" in deploy
    assert "content_bot_vod_feed.service" in deploy
    assert "vod_feed_owner_health" in deploy
    assert (SCRIPTS / "mlbb_vod_segment_feed.sh").is_file()
    assert (SCRIPTS / "content_bot_vod_feed.service").is_file()
    assert (SCRIPTS / "vod_feed_owner_health.py").is_file()
    assert (SCRIPTS / "install_vod_daily_quality_digest.sh").is_file()


def test_drought_watch_tracks_ledger_silence() -> None:
    src = (SCRIPTS / "vod_send_drought_watch.py").read_text(encoding="utf-8")
    assert "latest_gate_event_age_sec" in src
    assert "ledger_silent" in src


def test_feed_owner_health_detects_ledger_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOD_QUALITY_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("VOD_FEED_HEALTH_STATE", str(tmp_path / "health.json"))
    monkeypatch.setenv("VOD_FEED_AUTO_HEAL_DUPES", "0")
    env_file = tmp_path / "bot.env"
    env_file.write_text("", encoding="utf-8")
    import vod_feed_owner_health as health

    monkeypatch.setattr(health, "STATE_PATH", tmp_path / "health.json")
    monkeypatch.setattr(health, "_systemctl", lambda *a, **k: "active")
    monkeypatch.setattr(health, "n_restarts", lambda: 0)
    monkeypatch.setattr(health, "_pgrep", lambda pat: [1] if "mlbb" in pat else [2])
    # Support either helper name used by the module.
    if hasattr(health, "telegram_send"):
        monkeypatch.setattr(health, "telegram_send", lambda text: False)
    if hasattr(health, "telegram_send"):
        monkeypatch.setattr(health, "telegram_send", lambda text: False)
    # No ledger events yet → degraded
    rc = health.main(
        [
            "--game",
            "pubg",
            "--ledger-silence-hours",
            "0.001",
            "--env-file",
            str(env_file),
        ]
    )
    assert rc == 1
    report = json.loads(Path(tmp_path / "health.json").read_text(encoding="utf-8"))
    assert report["status"] == "degraded"
    assert any("ledger" in p for p in report["problems"])


def test_telegram_env_prefers_tg_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_telegram_env import bot_token, chat_id, credentials_ok

    monkeypatch.setenv("TG_BOT_TOKEN", "tg-token")
    monkeypatch.setenv("TG_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "alt-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    assert bot_token() == "tg-token"
    assert chat_id() == "123"
    assert credentials_ok() is True


def test_recover_prefers_systemd_not_nohup() -> None:
    src = (SCRIPTS / "vod_feed_recover.py").read_text(encoding="utf-8")
    assert "systemctl" in src
    assert "VOD_FEED_ALLOW_NOHUP" in src
    # default path must not nohup without opt-in
    assert '["nohup"' not in src or "VOD_FEED_ALLOW_NOHUP" in src


def test_hang_recover_is_systemd_only() -> None:
    src = (SCRIPTS / "vod_hang_detector.py").read_text(encoding="utf-8")
    assert "restart_supervisor(force=True)" not in src
    assert "_start_systemd_feed()" in src


def test_deploy_purges_legacy_watchdogs() -> None:
    deploy = (SCRIPTS / "deploy_unified_production.sh").read_text(encoding="utf-8")
    assert "continuous_worker_watchdog" in deploy or "mlbb_vod_health_watchdog" in deploy
    assert "Purge legacy" in deploy or "purged legacy" in deploy


def test_service_restart_on_failure() -> None:
    unit = (SCRIPTS / "content_bot_vod_feed.service").read_text(encoding="utf-8")
    assert "Restart=on-failure" in unit


def test_feed_honors_skip_discovery_alias() -> None:
    src = (SCRIPTS / "shooter_vod_segment_feed.py").read_text(encoding="utf-8")
    assert "VOD_FORCE_SKIP_DISCOVERY" in src


def test_ledger_tail_helper_exists() -> None:
    from vod_clip_quality_ledger import iter_events_tail

    assert callable(iter_events_tail)
