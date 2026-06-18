#!/usr/bin/env python3
"""Emergency: download fresh unsent Shorts and push to Telegram immediately."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gameplay_gate import is_mlbb_calibration_short
from mlbb_calibration_feed import main as run_feed
from mlbb_calibration_store import (
    SHORTS_ROOT,
    labeled_ids,
    load_feed_sent,
    pending_candidates,
    upsert_candidate,
)
from mlbb_youtube_shorts_ingest import download_short, fetch_upload_date, light_clip_features, search_shorts
from youtube_download import load_env

QUERIES = (
    "mlbb savage shorts",
    "mlbb teamfight shorts",
    "mobile legends highlights shorts",
    "mlbb triple kill shorts",
)


def main() -> int:
    env = {**os.environ, **load_env()}
    env["MLBB_CALIBRATION_LENIENT"] = "1"
    env["MLBB_CALIBRATION_FAST_INGEST"] = "1"
    os.environ.update(env)
    days = int(env.get("MLBB_SHORTS_DAYS", "60"))
    target = int(env.get("MLBB_BURST_TARGET", "6"))
    sent = load_feed_sent()["ids"]
    known = labeled_ids()
    saved = 0
    for query in QUERIES:
        for row in search_shorts(query, limit=25, env=env, days=days):
            vid = row["video_id"]
            if vid in sent or vid in known:
                continue
            mp4 = SHORTS_ROOT / f"yt_{vid}.mp4"
            if not mp4.exists():
                ud = str(row.get("upload_date") or "")
                if not ud or ud in ("NA", "N/A"):
                    ud = fetch_upload_date(vid, env)
                    if ud:
                        row["upload_date"] = ud
                min_year = int(env.get("MLBB_SHORTS_MIN_YEAR", "2024"))
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
                if ud and ud.isdigit() and len(ud) >= 8:
                    if int(ud[:4]) < min_year or ud < cutoff:
                        print(f"skip stale {vid} ud={ud}")
                        continue
                mp4 = download_short(row["url"], SHORTS_ROOT, env, vid, days=days) or mp4
            if not mp4.exists():
                print(f"skip download_fail {vid}")
                continue
            ok, gscore, reason = is_mlbb_calibration_short(mp4, description=row.get("title", ""))
            if not ok:
                print(f"skip gate {vid} {reason}")
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
            print(f"queued {vid} {row.get('title', '')[:55]}")
            saved += 1
            if saved >= target:
                break
        if saved >= target:
            break
    print(f"queued={saved} pending={len(pending_candidates(limit=9999))}")
    if saved > 0 or pending_candidates(limit=1):
        return run_feed()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
