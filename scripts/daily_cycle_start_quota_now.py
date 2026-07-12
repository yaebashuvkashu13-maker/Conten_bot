#!/usr/bin/env python3
"""Force fresh daily game quotas now (owner override — skip wait until MSK midnight)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from daily_game_cycle import start_next_quota_now, status_summary
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")


def main() -> int:
    env = load_env(ENV_PATH)
    os.environ.update(env)
    summary = start_next_quota_now(notify=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
