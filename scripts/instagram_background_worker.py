#!/usr/bin/env python3
"""Lightweight tick: only refresh IG config (full digest = cron 19:00 or /ig_digest)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo = Path("/root/content_bot_ml")
    builder = repo / "scripts/build_instagram_config.py"
    if not builder.exists():
        builder = Path("/usr/local/bin/build_instagram_config.py")
    return subprocess.run([sys.executable, str(builder)], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
