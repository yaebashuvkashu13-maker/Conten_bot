#!/usr/bin/env python3
"""Backfill scene_library_index from existing calibration labels."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_scene_library import backfill_from_labels, load_stats


def main() -> int:
    added = backfill_from_labels(skip_existing=True)
    stats = load_stats()
    print(f"backfill_added={added} stats={stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
