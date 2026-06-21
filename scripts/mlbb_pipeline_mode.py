#!/usr/bin/env python3
"""Shared MLBB pipeline mode flags (VOD-only vs Shorts calibration)."""

from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path("/root/.video_bot.env")


def _merged_env() -> dict[str, str]:
    env = dict(os.environ)
    if ENV_PATH.exists():
        try:
            from youtube_download import load_env

            env.update(load_env(ENV_PATH))
        except Exception:
            for line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                env.setdefault(key.strip(), val.strip().strip("'\""))
    return env


def vod_only_mode(env: dict[str, str] | None = None) -> bool:
    e = env if env is not None else _merged_env()
    return e.get("MLBB_VOD_ONLY", "0") == "1" and e.get("MLBB_VOD_DISABLED", "1") != "1"


def shorts_calibration_enabled(env: dict[str, str] | None = None) -> bool:
    e = env if env is not None else _merged_env()
    if vod_only_mode(e):
        return False
    return e.get("MLBB_CALIBRATION_FEED_ENABLED", "1") == "1"
