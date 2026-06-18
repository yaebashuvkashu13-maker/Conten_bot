#!/usr/bin/env python3
"""
MLBB-only: ingest fresh YouTube Shorts (≤60s, recent) for owner calibration.

Searches recent MLBB Shorts; rejects pre-2024 and stale uploads.
Output: /root/datasets/mlbb/youtube_shorts/ + data/mlbb/youtube_shorts_index.json
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gameplay_gate import OTHER_GAME_TITLE, is_mlbb_calibration_short, is_gameplay_video
from highlight_scorer import WINDOW_SEC, score_candidate_window
from mlbb_calibration_store import (
    SHORTS_ROOT,
    labeled_ids,
    load_feed_sent,
    pending_candidates,
    rebuild_index_from_disk,
    repair_index,
    upsert_candidate,
)
from viral_scorer import hook_score
from youtube_download import load_env, subprocess_env_no_proxy, ytdlp_cmd, ytdlp_extra_args

SEARCH_QUERIES = (
    "mlbb 2026 savage shorts",
    "mlbb 2025 highlights shorts",
    "mlbb mpl 2025 shorts",
    "mlbb m7 highlights shorts",
    "mlbb teamfight shorts",
    "mlbb savage shorts",
    "mobile legends highlights shorts",
    "mlbb triple kill shorts",
    "mlbb mythic rank fight shorts",
    "mobile legends esports highlights shorts",
    "mlbb onic alter ego shorts",
    "mlbb chou savage shorts",
)

HQ_FORMAT = (
    "bv*[height<=1080][height>=480]+ba/"
    "bv*[height<=1080]+ba/b[height<=1080]/best"
)

NEGATIVE_TITLE = re.compile(
    r"(#ad\b|sponsored|giveaway|promo\b|free\s+diamond|skin\s+gratis|"
    r"log\s*in\s+mlbb|mailbox|official\s+event|tutorial|guide|tips|"
    r"funny|meme|intro|reaction|rank\s+push\s+only|lobby|menu)",
    re.I,
)

PROFILE = "mobile_legends"


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


def _min_year(env: dict[str, str]) -> int:
    return int(env.get("MLBB_SHORTS_MIN_YEAR", "2024"))


def _sort_freshness_key(row: dict) -> tuple[str, int]:
    ud = str(row.get("upload_date") or "00000000")
    if not ud.isdigit():
        ud = "00000000"
    return (ud, int(row.get("view_count") or 0))


def fetch_upload_date(video_id: str, env: dict[str, str]) -> str:
    import subprocess

    cmd = ytdlp_cmd(env, use_proxy=False) + [
        f"https://www.youtube.com/watch?v={video_id}",
        "--skip-download",
        "--print",
        "upload_date",
        *ytdlp_extra_args(env),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=60, env=subprocess_env_no_proxy(env)
    )
    ud = (proc.stdout or "").strip()
    if ud.isdigit() and len(ud) >= 8:
        return ud
    return ""


def search_shorts(query: str, *, limit: int, env: dict[str, str], days: int) -> list[dict]:
    import subprocess

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
    min_year = _min_year(env)
    search_n = max(limit * 12, 120)
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        f"ytsearch{search_n}:{query} #shorts",
        "--flat-playlist",
        "--sleep-requests",
        env.get("YTDLP_SLEEP_REQUESTS", "1.5"),
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
        if not vid or len(vid) != 11:
            continue
        if upload_date and upload_date not in ("NA", "N/A") and upload_date.isdigit():
            if len(upload_date) >= 4 and int(upload_date[:4]) < min_year:
                continue
            if upload_date < cutoff:
                continue
        try:
            duration = float(dur or 0)
            view_count = int(float(views or 0))
        except (ValueError, TypeError):
            continue
        if duration <= 3 or duration > 60:
            continue
        if NEGATIVE_TITLE.search(title) or OTHER_GAME_TITLE.search(title):
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


def download_short(url: str, out_dir: Path, env: dict[str, str], video_id: str, *, days: int) -> Path | None:
    import subprocess

    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / "yt_%(id)s.%(ext)s")
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        "-f",
        env.get("YOUTUBE_SHORTS_FORMAT")
        or env.get("YOUTUBE_SHORTS_FORMAT_HQ", HQ_FORMAT),
        "--merge-output-format",
        "mp4",
        "--sleep-requests",
        env.get("YTDLP_SLEEP_REQUESTS", "1.5"),
        "--sleep-interval",
        env.get("YTDLP_SLEEP_INTERVAL", "4"),
        "--max-sleep-interval",
        env.get("YTDLP_MAX_SLEEP_INTERVAL", "12"),
        "-o",
        template,
        "--no-playlist",
        *ytdlp_extra_args(env),
        url,
    ]
    dest = out_dir / f"yt_{video_id}.mp4"
    if dest.exists():
        return dest
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=300, env=subprocess_env_no_proxy(env)
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[-240:]
        print(f"download_fail {video_id} rc={proc.returncode} {err}")
        return None
    if dest.exists():
        return dest
    for alt in out_dir.glob(f"yt_{video_id}.*"):
        if alt.suffix.lower() in (".mp4", ".mkv", ".webm") and alt.stat().st_size > 50_000:
            if alt != dest:
                try:
                    alt.rename(dest)
                except OSError:
                    return alt
            return dest
    return None


def light_clip_features(path: Path) -> dict:
    dur = _ffprobe_duration(path)
    return {
        "score": 0.18,
        "combat_score": 0.18,
        "clip_score": 0.0,
        "hook_score": 0.0,
        "panns_gun_max": 0.0,
        "minimap_delta": 0.0,
        "skill_delta": 0.0,
        "rule_pass": 1,
        "pass_reason": "calibration_fast",
        "hook_menu": 0,
        "duration": round(dur, 2),
    }


def score_clip(path: Path) -> dict:
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    dur = _ffprobe_duration(path)
    window = min(WINDOW_SEC, max(4.0, dur * 0.85))
    m = score_candidate_window(path, 0.15, window, PROFILE)
    hook, hook_meta = hook_score(path, 0.15, PROFILE, duration_sec=window)
    combat = (
        float(m.panns_gun_max) * 0.25
        + max(0.0, float(m.clip_score)) * 0.35
        + float(m.minimap_delta) * 2.0
        + float(m.skill_delta) * 2.0
        + hook * 0.25
    )
    gate = bool(m.rule_pass and m.visual_pass)
    combined = combat + (0.15 if gate else 0.0)
    return {
        "score": round(combined, 4),
        "combat_score": round(combat, 4),
        "clip_score": round(float(m.clip_score), 4),
        "hook_score": round(hook, 4),
        "panns_gun_max": round(float(m.panns_gun_max), 4),
        "minimap_delta": round(float(m.minimap_delta), 4),
        "skill_delta": round(float(m.skill_delta), 4),
        "rule_pass": int(gate),
        "pass_reason": m.pass_reason or "",
        "hook_menu": hook_meta.get("menu_overlay", 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-per-query", type=int, default=30)
    parser.add_argument("--days", type=int, default=int(os.environ.get("MLBB_SHORTS_DAYS", "60")))
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--min-score", type=float, default=0.12)
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Throttled cron mode: 1 query, few downloads, long pauses",
    )
    parser.add_argument("--max-downloads", type=int, default=0, help="0 = no limit")
    parser.add_argument("--download-delay", type=float, default=12.0)
    parser.add_argument("--search-delay", type=float, default=5.0)
    parser.add_argument(
        "--skip-if-pending",
        type=int,
        default=0,
        help="Skip YouTube if this many unevaluated candidates already queued",
    )
    args = parser.parse_args()

    if args.incremental:
        if args.max_downloads <= 0:
            args.max_downloads = int(os.environ.get("MLBB_INGEST_MAX_DOWNLOADS", "3"))
        if args.max_per_query > 12:
            args.max_per_query = 12
        if args.skip_if_pending <= 0:
            args.skip_if_pending = int(os.environ.get("MLBB_INGEST_SKIP_IF_PENDING", "12"))

    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("CONTENT_BOT_REPO", "/root/content_bot_ml")
    env = {**os.environ, **load_env()}
    SHORTS_ROOT.mkdir(parents=True, exist_ok=True)
    pruned = repair_index()
    if pruned:
        print(f"repair_index removed={pruned}")
    rebuilt = rebuild_index_from_disk()
    if rebuilt:
        print(f"rebuild_index_from_disk added={rebuilt}")

    pending_n = len(pending_candidates(limit=9999))
    if args.skip_if_pending > 0 and pending_n >= args.skip_if_pending:
        print(f"SKIP ingest pending={pending_n} >= {args.skip_if_pending} (no YouTube calls)")
        return 0

    queries = list(SEARCH_QUERIES)
    if args.incremental:
        # Rotate one query per hour — steady fresh discovery without hammering YouTube.
        slot = int(time.time() // 3600) % len(queries)
        queries = [queries[slot]]
        print(f"incremental query={queries[0]} pending={pending_n} days={args.days}")

    seen: set[str] = set()
    pool: list[dict] = []
    for query in queries:
        for row in search_shorts(query, limit=args.max_per_query, env=env, days=args.days):
            vid = row["video_id"]
            if vid in seen:
                continue
            seen.add(vid)
            pool.append(row)
        if args.search_delay > 0 and len(queries) > 1:
            time.sleep(args.search_delay)

    pool.sort(key=_sort_freshness_key, reverse=True)
    cap = args.max_per_query * len(queries)
    pool = pool[: cap * 3]  # extra headroom — many rows already labeled

    known = labeled_ids()
    already_sent = load_feed_sent()["ids"]
    sent_pending = {str(r.get("video_id", "")) for r in pending_candidates(limit=9999)}
    fresh_pool: list[dict] = []
    for row in pool:
        vid = row["video_id"]
        if vid in known or vid in already_sent or vid in sent_pending:
            continue
        fresh_pool.append(row)
    pool = fresh_pool[:cap]

    if not pool and args.incremental:
        deep: list[dict] = []
        extra_queries = list(SEARCH_QUERIES)
        slot = int(time.time() // 3600) % len(extra_queries)
        extra_queries = extra_queries[slot:] + extra_queries[:slot]
        for query in extra_queries[:4]:
            for row in search_shorts(
                query,
                limit=max(args.max_per_query * 3, 24),
                env=env,
                days=args.days,
            ):
                vid = row["video_id"]
                if vid in known or vid in already_sent or vid in sent_pending:
                    continue
                deep.append(row)
            if len(deep) >= cap:
                break
            if args.search_delay > 0:
                time.sleep(args.search_delay)
        pool = sorted(deep, key=_sort_freshness_key, reverse=True)[: cap * 2]

    saved = rejected = downloads = skipped_known = 0
    lenient = os.environ.get("MLBB_CALIBRATION_LENIENT", "1") == "1"
    fast_ingest = os.environ.get("MLBB_CALIBRATION_FAST_INGEST", "1") == "1"
    for row in pool:
        if args.max_downloads > 0 and downloads >= args.max_downloads:
            break
        vid = row["video_id"]
        if vid in known or vid in already_sent:
            skipped_known += 1
            continue
        mp4 = SHORTS_ROOT / f"yt_{vid}.mp4"
        if not mp4.exists() and not args.skip_download:
            ud = str(row.get("upload_date") or "")
            if not ud or ud in ("NA", "N/A"):
                ud = fetch_upload_date(vid, env)
                if ud:
                    row["upload_date"] = ud
            min_year = int(env.get("MLBB_SHORTS_MIN_YEAR", "2024"))
            cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y%m%d")
            if ud and ud.isdigit() and len(ud) >= 8:
                if int(ud[:4]) < min_year or ud < cutoff:
                    rejected += 1
                    continue
            mp4 = download_short(row["url"], SHORTS_ROOT, env, vid, days=args.days) or mp4
            downloads += 1
            time.sleep(max(2.0, args.download_delay))
        if not mp4.exists() or mp4.name != f"yt_{vid}.mp4":
            continue

        ok, gscore, reason = is_mlbb_calibration_short(mp4, description=row.get("title", ""))
        hard_reject = reason in ("promo_text", "csv_lookup", "other_game_title", "promo_edit")
        if not ok:
            if hard_reject or not lenient:
                rejected += 1
                continue

        if fast_ingest and lenient:
            feats = light_clip_features(mp4)
            feats["score"] = max(float(feats.get("score") or 0), float(gscore))
        else:
            feats = score_clip(mp4)
        if feats["score"] < args.min_score and not feats["rule_pass"] and not lenient:
            rejected += 1
            continue
        if not ok and lenient and feats["score"] < 0.05:
            rejected += 1
            continue

        upsert_candidate(
            {
                **row,
                **feats,
                "path": str(mp4),
                "gameplay_pass": int(ok),
                "gameplay_score": round(float(gscore), 4),
                "gameplay_reason": reason,
                "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        saved += 1
        print(f"OK {vid} score={feats['score']:.3f} views={row.get('view_count')} {row.get('title','')[:50]}")

    print(
        f"SUMMARY saved={saved} rejected={rejected} downloads={downloads} skipped_known={skipped_known} "
        f"pool={len(pool)} pending={pending_n} dir={SHORTS_ROOT}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
