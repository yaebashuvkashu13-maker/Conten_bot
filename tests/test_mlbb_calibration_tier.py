from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mlbb_calibration_tier import resolve_tier, tier_env  # noqa: E402


def test_resolve_tier_healthy_queue() -> None:
    assert resolve_tier(pending=20, state={}, now=1000.0) == 0


def test_resolve_tier_escalates_when_empty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path))
    import mlbb_calibration_tier as tier_mod

    monkeypatch.setattr(tier_mod, "STATE_PATH", tmp_path / "tier_state.json")
    now = 10_000.0
    state = {"empty_since": now - 200.0}
    assert tier_mod.resolve_tier(pending=0, state=state, now=now) == 1
    state = {"empty_since": now - 400.0}
    assert tier_mod.resolve_tier(pending=0, state=state, now=now) == 2
    state = {"empty_since": now - 500.0}
    assert tier_mod.resolve_tier(pending=0, state=state, now=now) == 3


def test_tier_env_relaxes_kill_at_starvation() -> None:
    assert tier_env(3)["MLBB_SHORTS_REQUIRE_KILL_UI"] == "0"
    assert tier_env(0)["MLBB_CALIBRATION_LENIENT"] == "0"
