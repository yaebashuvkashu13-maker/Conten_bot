#!/usr/bin/env python3
"""Launcher quality exports must survive daily_cycle_runner env reload."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_load_runtime_env_preserves_launcher_auto_download(monkeypatch, tmp_path) -> None:
    import daily_cycle_runner as dcr

    env_file = tmp_path / "video_bot.env"
    env_file.write_text("MLBB_VOD_AUTO_DOWNLOAD=0\nMLBB_OCR_DOUBLE_REQUIRE_LIVE=0\n")
    monkeypatch.setattr(dcr, "ENV_PATH", env_file)
    monkeypatch.setenv("MLBB_VOD_AUTO_DOWNLOAD", "1")
    monkeypatch.setenv("MLBB_OCR_DOUBLE_REQUIRE_LIVE", "1")
    monkeypatch.setenv("MLBB_BANNER_OWN_HUD_MIN_SIM", "0.19")

    with patch.object(dcr, "load_env", side_effect=lambda p: {
        "MLBB_VOD_AUTO_DOWNLOAD": "0",
        "MLBB_OCR_DOUBLE_REQUIRE_LIVE": "0",
        "TG_BOT_TOKEN": "x",
    }):
        env = dcr._load_runtime_env()

    assert env["MLBB_VOD_AUTO_DOWNLOAD"] == "1"
    assert env["MLBB_OCR_DOUBLE_REQUIRE_LIVE"] == "1"
    assert env["MLBB_BANNER_OWN_HUD_MIN_SIM"] == "0.19"
