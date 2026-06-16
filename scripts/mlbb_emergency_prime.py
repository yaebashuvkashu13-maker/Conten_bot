#!/usr/bin/env python3
"""Autonomous queue refill when pending=0 — download fresh Shorts + send to Telegram."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_calibration_store import (
    index_unlabeled_disk_shorts,
    ingest_pool_skip_ids,
    pending_candidates,
    sendable_pending_count,
    stats,
)
from mlbb_youtube_shorts_ingest import download_short, search_shorts
from youtube_download import load_env, subprocess_env_no_proxy

BIN = Path("/usr/local/bin")
PY = sys.executable

FRESH_QUERIES = (
    "mlbb savage 2026",
    "mobile legends epic savage shorts",
    "mlbb maniac double kill",
    "mlbb ranked mythic highlights",
    "mobile legends fighter savage",
    "mlbb teamfight savage moment",
    "mlbb pentakill shorts",
    "mobile legends bang bang savage",
    "mlbb hook savage",
    "mlbb epic comeback ranked",
)


def _base_env() -> dict[str, str]:
    env = load_env(Path("/root/.video_bot.env"))
    env.setdefault("PYTHONPATH", str(BIN))
    env.setdefault("YTDLP_REMOTE_COMPONENTS", "ejs:github")
    env.setdefault("MLBB_SHORTS_ONLY", "1")
    env.setdefault("MLBB_SHORTS_REQUIRE_KILL_UI", "0")
    env.setdefault("MLBB_CALIBRATION_LENIENT", "1")
    env["PATH"] = subprocess_env_no_proxy(env)["PATH"]
    return env


def discover_fresh(*, env: dict[str, str], limit: int = 12) -> list[dict]:
    skip = ingest_pool_skip_ids()
    slot = int(time.time() // 600) % len(FRESH_QUERIES)
    ordered = [FRESH_QUERIES[(slot + i) % len(FRESH_QUERIES)] for i in range(len(FRESH_QUERIES))]
    out: list[dict] = []
    seen: set[str] = set()
    for query in ordered:
        rows = search_shorts(query, limit=40, env=env, days=730, force_shorts=True)
        for row in rows:
            vid = str(row.get("video_id", ""))
            if not vid or len(vid) != 11 or vid in skip or vid in seen:
                continue
            seen.add(vid)
            out.append(row)
            if len(out) >= limit:
                return out
    return out


def prime_queue(*, max_downloads: int = 4, run_feed: bool = True) -> dict:
    env = _base_env()
    shorts_root = Path(env.get("MLBB_SHORTS_ROOT", "/root/datasets/mlbb/youtube_shorts"))
    before = sendable_pending_count(limit=500, repair=False)
    if before >= int(env.get("MLBB_EMERGENCY_SKIP_IF_PENDING", "2")):
        return {"ok": True, "skipped": True, "pending_before": before}

    fresh = discover_fresh(env=env, limit=max_downloads * 4)
    downloaded = 0
    for row in fresh[: max_downloads * 2]:
        if downloaded >= max_downloads:
            break
        vid = row["video_id"]
        dest = shorts_root / f"yt_{vid}.mp4"
        if dest.exists() and dest.stat().st_size > 10_000:
            downloaded += 1
            continue
        url = row.get("url") or f"https://www.youtube.com/shorts/{vid}"
        got = download_short(url, shorts_root, env, vid)
        if got and got.exists():
            downloaded += 1
            print(f"emergency_download ok {vid}", flush=True)
        else:
            print(f"emergency_download fail {vid}", flush=True)

    indexed = index_unlabeled_disk_shorts(limit=max_downloads * 2)
    after = sendable_pending_count(limit=500, repair=False)
    sent = 0
    if run_feed and after > before:
        feed = BIN / "mlbb_calibration_feed.py"
        if not feed.exists():
            feed = Path(__file__).resolve().parent / "mlbb_calibration_feed.py"
        proc = subprocess.run(
            [PY, str(feed)],
            env={**env, "MLBB_FEED_REBUILD": "1", "MLBB_SHORTS_REQUIRE_KILL_UI": "0"},
            timeout=int(env.get("MLBB_HEALTH_FEED_TIMEOUT", "300")),
            check=False,
        )
        sent = 1 if proc.returncode == 0 else 0

    result = {
        "ok": after > before or downloaded > 0,
        "pending_before": before,
        "pending_after": after,
        "fresh_found": len(fresh),
        "downloaded": downloaded,
        "indexed": indexed,
        "feed_rc": sent,
        "stats": stats(),
    }
    print(f"emergency_prime {result}", flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Emergency MLBB Shorts queue prime")
    parser.add_argument("--max-downloads", type=int, default=0)
    parser.add_argument("--no-feed", action="store_true")
    args = parser.parse_args()
    env = _base_env()
    max_dl = args.max_downloads or int(env.get("MLBB_EMERGENCY_MAX_DOWNLOADS", "4"))
    prime_queue(max_downloads=max_dl, run_feed=not args.no_feed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
