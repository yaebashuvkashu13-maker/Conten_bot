#!/usr/bin/env python3
"""Re-queue PUBG inbox VODs wrongly marked exhausted — delegates to reset_vod_inbox_exhausted."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reset_vod_inbox_exhausted import reset_game


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Reset exhausted PUBG inbox VODs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--metro-reject-only", action="store_true")
    args = parser.parse_args()
    reset_game("pubg", dry_run=args.dry_run, gate_reject_only=args.metro_reject_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
