#!/usr/bin/env python3
"""One-off: download a few unsent fresh Shorts into the calibration queue."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_calibration_store import (
    SHORTS_ROOT,
    labeled_ids,
    load_feed_sent,
    pending_candidates,
    upsert_candidate,
)
from mlbb_youtube_shorts_ingest import download_short, light_clip_features, search_shorts
from gameplay_gate import is_mlbb_calibration_short
from youtube_download import load_env

QUERIES = (
    "mlbb savage shorts",
    "mlbb teamfight shorts",
    "mlbb 2026 savage shorts",
)


def main() -> int:
    env = {**os.environ, **load_env()}
    days = int(env.get("MLBB_SHORTS_DAYS", "60"))
    limit = int(env.get("MLBB_BOOTSTRAP_MAX", "6"))
    sent = load_feed_sent()["ids"]
    known = labeled_ids()
    saved = 0
    for query in QUERIES:
        for row in search_shorts(query, limit=20, env=env, days=days):
            vid = row["video_id"]
            if vid in sent or vid in known:
                continue
            mp4 = download_short(row["url"], SHORTS_ROOT, env, vid, days=days)
            if not mp4 or not mp4.exists():
                continue
            ok, gscore, reason = is_mlbb_calibration_short(
                mp4, description=row.get("title", "")
            )
            if not ok:
                print(f"skip non-mlbb {vid} reason={reason}")
                continue
            upsert_candidate(
                {
                    **row,
                    **light_clip_features(mp4),
                    "path": str(mp4),
                    "gameplay_pass": 1,
                    "gameplay_score": round(float(gscore), 4),
                    "gameplay_reason": reason,
                    "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            print(f"saved {vid} {row.get('title', '')[:60]}")
            saved += 1
            if saved >= limit:
                break
        if saved >= limit:
            break
    print(f"pending={len(pending_candidates(limit=9999))} saved={saved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
