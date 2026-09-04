from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pubg_owner_calibration import apply_owner_send_policy  # noqa: E402


def test_apply_owner_send_policy_sets_strict_combat_defaults(monkeypatch) -> None:
    for key in (
        "PUBG_PRESEND_SHOOTING_GATE",
        "PUBG_EARLY_PAYOFF_REJECT_SINGLES",
        "PUBG_SINGLE_MIN_GUN_DENSITY",
        "PUBG_REJECT_LOOT_WALK",
        "PUBG_PAYOFF_SCORE_MIN_SINGLES",
        "PUBG_QUALITY_SCORE_MIN_SINGLES",
        "PUBG_SINGLES_GUN_PAYOFF_BYPASS",
        "PUBG_SINGLES_GUN_QUALITY_BYPASS",
    ):
        monkeypatch.delenv(key, raising=False)
    apply_owner_send_policy()
    env = __import__("os").environ
    assert env["PUBG_PRESEND_SHOOTING_GATE"] == "1"
    assert env["PUBG_EARLY_PAYOFF_REJECT_SINGLES"] == "1"
    assert float(env["PUBG_SINGLE_MIN_GUN_DENSITY"]) >= 0.070
    assert float(env["PUBG_PAYOFF_SCORE_MIN_SINGLES"]) >= 0.38
    assert float(env["PUBG_QUALITY_SCORE_MIN_SINGLES"]) >= 0.48
    assert env["PUBG_SINGLES_GUN_PAYOFF_BYPASS"] == "0"
    assert env["PUBG_SINGLES_GUN_QUALITY_BYPASS"] == "0"
    assert env["PUBG_REJECT_LOOT_WALK"] == "1"
