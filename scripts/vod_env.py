#!/usr/bin/env python3
"""Single source for loading /root/.video_bot.env (VOD pipeline)."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_PATH = Path(os.environ.get("VIDEO_BOT_ENV", "/root/.video_bot.env"))


def load_env(path: Path | None = None) -> dict[str, str]:
    """Parse KEY=VALUE env file; strip quotes. Does not mutate os.environ."""
    env_path = path or DEFAULT_ENV_PATH
    env: dict[str, str] = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env
