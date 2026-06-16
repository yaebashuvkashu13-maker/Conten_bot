from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load_health():
    spec = importlib.util.spec_from_file_location("mph", SCRIPTS / "mlbb_pipeline_health.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_steady_feed_pacing(tmp_path: Path, monkeypatch) -> None:
    health = tmp_path / "health.json"
    monkeypatch.setenv("MLBB_PIPELINE_HEALTH", str(health))
    monkeypatch.setenv("MLBB_STEADY_MODE", "1")
    monkeypatch.setenv("MLBB_STEADY_FEED_INTERVAL_SEC", "600")
    monkeypatch.setenv("MLBB_STEADY_FORCE_SEND_SILENCE_SEC", "3600")
    mph = _load_health()

    mph.record_feed_delivery(delivered=3)
    ok, reason = mph.should_send_feed_steady(pending=8, batch_size=4)
    assert ok  # full batch — send even before interval

    ok_partial, reason_partial = mph.should_send_feed_steady(pending=2, batch_size=4)
    assert not ok_partial
    assert "steady_wait" in reason_partial

    data = mph._read()
    data["last_feed_delivered_at"] = time.time() - 700
    mph._write(data)
    ok2, _ = mph.should_send_feed_steady(pending=2, batch_size=4)
    assert ok2  # interval elapsed — send partial batch


def test_unsendable_feed_recovery(tmp_path: Path, monkeypatch) -> None:
    health = tmp_path / "health.json"
    monkeypatch.setenv("MLBB_PIPELINE_HEALTH", str(health))
    monkeypatch.setenv("MLBB_UNSENDABLE_FEED_RECOVERY", "3")
    mph = _load_health()

    for _ in range(3):
        mph.record_feed_delivery(delivered=0, skipped_unsendable=2)
    need, reason = mph.needs_recovery(pending=1)
    assert need
    assert "unsendable_feed_streak" in reason


def test_needs_recovery_on_silence(tmp_path: Path, monkeypatch) -> None:
    health = tmp_path / "health.json"
    monkeypatch.setenv("MLBB_PIPELINE_HEALTH", str(health))
    monkeypatch.setenv("MLBB_MAX_SILENCE_SEC", "3600")
    mph = _load_health()

    mph.record_feed_delivery(delivered=2)
    data = mph._read()
    data["last_feed_delivered_at"] = time.time() - 4000
    mph._write(data)

    need, reason = mph.needs_recovery(pending=0)
    assert need
    assert "no_delivery" in reason
