"""Regression: WoT/Genshin VOD 👍/👎 callbacks must be routed."""

from __future__ import annotations

from pathlib import Path


def test_shooter_vseg_callback_loop_includes_wot_and_genshin() -> None:
    src = (Path(__file__).resolve().parents[1] / "scripts" / "telegram_upload_bot.py").read_text()
    assert "for shooter_game in ('pubg', 'standoff', 'genshin', 'wot')" in src
    assert "if data.startswith(f'{prefix}_vseg_'):" in src
    assert "return False" in src.split("if data.startswith(f'{prefix}_vseg_'):", 1)[1][:80]
