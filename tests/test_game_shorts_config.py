"""Load shorts calibration game config."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from game_shorts_calibration import load_games  # noqa: E402


def test_five_games_loaded():
    games = load_games()
    ids = {g.id for g in games}
    assert ids == {"mlbb", "pubg", "standoff", "wot", "genshin"}


def test_pubg_queries_metro_royal():
    pubg = next(g for g in load_games() if g.id == "pubg")
    joined = " ".join(pubg.queries).lower()
    assert "метро роял" in joined
    assert "7 карта" in joined
    assert "фул 6" in joined
