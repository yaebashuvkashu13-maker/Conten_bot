#!/usr/bin/env python3
"""Ingest PUBG / Metro Royale YouTube Shorts for owner calibration."""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pubg_shorts_calibration_store import (
    SHORTS_ROOT,
    ingest_sent_blocklist,
    labeled_ids,
    pending_candidates,
    rebuild_index_from_disk,
    repair_index,
    upsert_candidate,
)
from pubg_shorts_title_gate import pubg_short_passes_calibration, pubg_short_title_ok
from youtube_download import load_env, subprocess_env_no_proxy, ytdlp_cmd, ytdlp_extra_args

SEARCH_QUERIES = (
    "PUBG Mobile Metro Royale shorts",
    "PUBG Metro Royale fight shorts",
    "PUBG Mobile Metro Royale gameplay shorts",
    "метро рояль пабг шортс",
    "PUBG Mobile combat shorts",
    "PUBG Mobile sniper shorts",
    "PUBG Metro Royale clutch shorts",
    "PUBG Mobile ranked shorts",
    "пабг метро рояль клип",
    "PUBG Mobile POV shorts",
)

HQ_FORMAT = "bv*[height<=1080][height>=480]+ba/b[height<=1080]/best"


def _ffprobe_duration(path: Path) -> float:
    import subprocess

    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    try:
        return float((proc.stdout or "0").strip())
    except ValueError:
        return 0.0


def search_shorts(
    query: str,
    *,
    limit: int,
    env: dict[str, str],
    days: int,
    skip_ids: set[str] | None = None,
) -> list[dict]:
    import subprocess

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
    min_year = int(env.get("PUBG_SHORTS_MIN_YEAR", "2024"))
    skip = skip_ids or set()
    depth = int(env.get("PUBG_SHORTS_SEARCH_DEPTH", "80"))
    search_n = min(depth, max(limit * 6, 40))
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        f"ytsearch{search_n}:{query} #shorts",
        "--flat-playlist",
        "--sleep-requests",
        env.get("YTDLP_SLEEP_REQUESTS", "1.0"),
        "--print",
        "%(id)s\t%(title)s\t%(view_count)s\t%(duration)s\t%(upload_date)s\t%(webpage_url)s",
        "--no-download",
        *ytdlp_extra_args(env),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=180, env=subprocess_env_no_proxy(env)
    )
    entries: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        vid, title, views, dur, upload_date, url = parts[:6]
        if not vid or len(vid) != 11 or vid in skip:
            continue
        if upload_date and upload_date.isdigit() and len(upload_date) >= 4:
            if int(upload_date[:4]) < min_year or upload_date < cutoff:
                continue
        try:
            duration = float(dur or 0)
            view_count = int(float(views or 0))
        except (ValueError, TypeError):
            continue
        if duration <= 2.5 or duration > 60:
            continue
        if view_count < int(env.get("PUBG_SHORTS_MIN_VIEWS", "100")):
            continue
        if not pubg_short_title_ok(title):
            continue
        entries.append(
            {
                "video_id": vid,
                "title": title[:240],
                "view_count": view_count,
                "duration": duration,
                "upload_date": upload_date,
                "url": url or f"https://www.youtube.com/shorts/{vid}",
                "search_query": query,
            }
        )
        if len(entries) >= limit:
            break
    return entries


def download_short(url: str, out_dir: Path, env: dict[str, str], video_id: str) -> Path | None:
    import subprocess

    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / "yt_%(id)s.%(ext)s")
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        "-f",
        env.get("YOUTUBE_SHORTS_FORMAT") or env.get("YOUTUBE_SHORTS_FORMAT_HQ", HQ_FORMAT),
        "--merge-output-format",
        "mp4",
        "--sleep-requests",
        "1",
        "-o",
        template,
        "--no-playlist",
        *ytdlp_extra_args(env),
        url,
    ]
    dest = out_dir / f"yt_{video_id}.mp4"
    if dest.exists() and dest.stat().st_size > 8000:
        return dest
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=300, env=subprocess_env_no_proxy(env)
    )
    if proc.returncode != 0:
        print(f"download_fail {video_id} {(proc.stderr or '')[-180:]}")
        return None
    if dest.exists() and _ffprobe_duration(dest) > 0:
        return dest
    for alt in out_dir.glob(f"yt_{video_id}.*"):
        if alt.suffix == ".mp4" and alt.stat().st_size > 8000:
            return alt
    return None


def main() -> int:
    lock_path = Path("/tmp/pubg_youtube_shorts_ingest.lock")
    lock_fd = lock_path.open("w")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("skip ingest another instance running")
        return 0

    parser = argparse.ArgumentParser()
    parser.add_argument("--max-per-query", type=int, default=15)
    parser.add_argument("--days", type=int, default=int(os.environ.get("PUBG_SHORTS_DAYS", "365")))
    parser.add_argument("--max-downloads", type=int, default=0)
    parser.add_argument("--download-delay", type=float, default=3.0)
    parser.add_argument("--search-delay", type=float, default=2.0)
    args = parser.parse_args()

    env = {**os.environ, **load_env()}
    os.environ.setdefault("PUBG_METRO_GATE", "1")
    SHORTS_ROOT.mkdir(parents=True, exist_ok=True)
    repair_index()
    rebuild_index_from_disk()

    known = labeled_ids()
    blocklist = ingest_sent_blocklist()
    slot = int(time.time() // 180) % len(SEARCH_QUERIES)
    queries = SEARCH_QUERIES[slot:] + SEARCH_QUERIES[:slot]
    if int(env.get("PUBG_INGEST_HUNGRY", "0")) == "1":
        queries = list(SEARCH_QUERIES)

    pool: list[dict] = []
    seen: set[str] = set()
    for query in queries:
        for row in search_shorts(
            query,
            limit=args.max_per_query,
            env=env,
            days=args.days,
            skip_ids=blocklist | seen | set(known),
        ):
            vid = row["video_id"]
            if vid in seen:
                continue
            seen.add(vid)
            pool.append(row)
        if args.search_delay > 0:
            time.sleep(args.search_delay)
        if len(pool) >= args.max_per_query * 4:
            break

    max_dl = args.max_downloads or int(env.get("PUBG_INGEST_MAX_DOWNLOADS", "15"))
    saved = rejected = downloads = 0
    for row in pool:
        if downloads >= max_dl:
            break
        vid = row["video_id"]
        title = str(row.get("title", ""))
        mp4 = SHORTS_ROOT / f"yt_{vid}.mp4"
        if not mp4.exists():
            mp4 = download_short(row["url"], SHORTS_ROOT, env, vid) or mp4
            if mp4.exists():
                downloads += 1
                time.sleep(max(1.0, args.download_delay))
        if not mp4.exists():
            rejected += 1
            continue
        ok, gscore, reason = pubg_short_passes_calibration(mp4, title=title)
        if not ok:
            rejected += 1
            print(f"REJECT {vid} {reason}")
            continue
        upsert_candidate(
            {
                **row,
                "path": str(mp4),
                "gameplay_pass": 1,
                "gameplay_score": round(float(gscore), 4),
                "gameplay_reason": reason,
                "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        saved += 1
        print(f"queued {vid} {title[:50]} ({reason})")

    print(
        f"ingest done saved={saved} rejected={rejected} downloads={downloads} "
        f"pending={len(pending_candidates(limit=9999, repair=False))}"
    )
    return 0 if saved or pending_candidates(limit=1, repair=False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
