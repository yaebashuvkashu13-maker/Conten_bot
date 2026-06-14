#!/usr/bin/env python3
"""Run all Shorts gates on pending queue — drop wrong-game / junk already indexed."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_calibration_store import pending_candidates, reject_candidate
from mlbb_youtube_shorts_ingest import (
    passes_mlbb_shorts_activity_gate,
    passes_mlbb_shorts_gameplay_gate,
    passes_mlbb_shorts_identity_gate,
    passes_mlbb_shorts_verify_gate,
)

CHECKS = (
    passes_mlbb_shorts_identity_gate,
    passes_mlbb_shorts_activity_gate,
    passes_mlbb_shorts_gameplay_gate,
    passes_mlbb_shorts_verify_gate,
)


def main() -> int:
    removed = 0
    kept = 0
    for row in pending_candidates(limit=9999):
        path = Path(row.get("path", ""))
        vid = str(row.get("video_id", ""))
        if not path.exists() or not vid:
            continue
        title = str(row.get("title", ""))
        for check in CHECKS:
            ok, reason = check(path, title=title)
            if not ok:
                reject_candidate(vid, reason=reason, path=path)
                print(f"purge {vid} {reason}")
                removed += 1
                break
        else:
            kept += 1
    print(f"SUMMARY purge removed={removed} kept={kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
