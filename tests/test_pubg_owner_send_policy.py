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
    ):
        monkeypatch.delenv(key, raising=False)
    apply_owner_send_policy()
    assert __import__("os").environ["PUBG_PRESEND_SHOOTING_GATE"] == "1"
    assert __import__("os").environ["PUBG_EARLY_PAYOFF_REJECT_SINGLES"] == "0"
    assert float(__import__("os").environ["PUBG_SINGLE_MIN_GUN_DENSITY"]) >= 0.045
    assert float(__import__("os").environ["PUBG_PAYOFF_SCORE_MIN_SINGLES"]) >= 0.16
