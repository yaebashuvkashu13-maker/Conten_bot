"""Regression tests for hang heal schedule + drought age wiring."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_drought_last_send_age_calls_hang_without_game_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    import vod_send_drought_watch as drought
    import vod_hang_detector as hang

    calls: list[tuple] = []

    def fake_age(*args, **kwargs):
        calls.append((args, kwargs))
        return 7200.0

    monkeypatch.setattr(hang, "last_send_age_sec", fake_age)
    # Ensure import path uses our patched module
    monkeypatch.setitem(sys.modules, "vod_hang_detector", hang)

    age = drought.last_send_age_sec("pubg")
    assert age == 7200.0
    assert calls == [((), {})], f"hang last_send_age_sec must be called with no args, got {calls}"


def test_drought_escalates_to_hang_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    import vod_send_drought_watch as drought

    monkeypatch.setenv("VOD_DROUGHT_TRIGGER_HANG", "1")
    monkeypatch.setenv("VOD_DROUGHT_AUTO_RECOVER", "1")

    class Boom(Exception):
        pass

    def fake_systemctl(*_a, **_k):
        raise Boom("no systemd in test")

    monkeypatch.setattr(drought.subprocess if hasattr(drought, "subprocess") else __import__("subprocess"), "run", fake_systemctl, raising=False)

    # Patch subprocess.run used inside _trigger_hang_tick
    import subprocess as sp

    def fail_run(*_a, **_k):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(sp, "run", fail_run)

    ticks: list[dict] = []

    def fake_tick(*, game: str = "pubg", force: bool = False):
        ticks.append({"game": game, "force": force})
        return {"ok": False, "heal": {"action": "full_recover_bg"}}

    import vod_hang_detector as hang

    monkeypatch.setattr(hang, "run_tick", fake_tick)
    monkeypatch.setitem(sys.modules, "vod_hang_detector", hang)

    # Skip inbox recover
    class FakeInbox:
        @staticmethod
        def drop_live_stubs(game):
            return []

        @staticmethod
        def unpark_recent(game, limit=5):
            return []

        @staticmethod
        def clear_exhausted(game, names=None):
            return 0

    monkeypatch.setitem(sys.modules, "vod_inbox_recover", FakeInbox)

    out = drought.maybe_recover("pubg")
    assert out["hang"]["via"] == "inline"
    assert ticks and ticks[0]["game"] == "pubg"


def test_hang_unit_files_exist_and_tick() -> None:
    root = SCRIPTS
    unit = (root / "content_bot_vod_hang.service").read_text(encoding="utf-8")
    timer = (root / "content_bot_vod_hang.timer").read_text(encoding="utf-8")
    assert "--tick" in unit
    assert "vod_hang_detector.py" in unit
    assert "VOD_FEED_ALLOW_NOHUP=0" in unit
    assert "OnUnitActiveSec=" in timer or "OnCalendar=" in timer
    install = (root / "install_vod_hang_watch.sh").read_text(encoding="utf-8")
    assert "content-bot-vod-hang.timer" in install
    deploy = (root / "deploy_unified_production.sh").read_text(encoding="utf-8")
    assert "install_vod_hang_watch.sh" in deploy
    assert "VOD_PUBG_ONLY" in deploy


def test_hang_loads_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vod_hang_detector as hang

    env = tmp_path / "bot.env"
    env.write_text("TG_BOT_TOKEN=test-token\nTG_CHAT_ID=123\n", encoding="utf-8")
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    hang._load_env_file(env)
    assert hang.os.environ.get("TG_BOT_TOKEN") == "test-token"
    assert hang.os.environ.get("TG_CHAT_ID") == "123"


def test_feed_lock_miss_exits_nonzero() -> None:
    text = (SCRIPTS / "shooter_vod_segment_feed.py").read_text(encoding="utf-8")
    assert "if lock is None:" in text
    idx = text.index("if lock is None:")
    snippet = text[idx : idx + 280]
    assert "return 1" in snippet
    assert "return 0" not in snippet.split("return 1", 1)[0]
