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


def _needs_quoting(value: str) -> bool:
    return any(ch in value for ch in "[]<>*?|&;()$`\\\"' ")


def format_env_line(key: str, value: str) -> str:
    """Safe KEY=VALUE line for bash source (yt-dlp format strings contain [])."""
    if _needs_quoting(value):
        escaped = value.replace("'", "'\"'\"'")
        return f"{key}='{escaped}'"
    return f"{key}={value}"


def set_env_kv(path: Path, key: str, value: str) -> None:
    """Upsert one env key with shell-safe quoting."""
    path = path or DEFAULT_ENV_PATH
    line = format_env_line(key, value)
    lines: list[str] = []
    found = False
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.strip().startswith(f"{key}="):
                if not found:
                    lines.append(line)
                    found = True
                continue
            lines.append(raw)
    if not found:
        lines.append(line)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
