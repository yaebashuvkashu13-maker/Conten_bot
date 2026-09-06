#!/usr/bin/env python3
"""Light self-heal entry for cycle_stall_watchdog cron (VOD-only compatible)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    # In VOD-only / PUBG-only mode the real healer is vod_hang_detector.
    if os.environ.get("MLBB_VOD_ONLY", "0") == "1" or os.environ.get("VOD_PUBG_ONLY", "0") == "1":
        from vod_hang_detector import run_tick

        result = run_tick(game="pubg", force=False)
        print(result)
        return 0
    # Legacy multi-game path: if missing helpers, do nothing.
    try:
        from vod_hang_detector import run_tick

        print(run_tick(game="all", force=False))
    except Exception as exc:
        print(f"cycle_self_heal skip: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
