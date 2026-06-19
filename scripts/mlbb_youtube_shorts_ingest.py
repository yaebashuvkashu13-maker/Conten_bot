#!/usr/bin/env python3
"""
MLBB-only: ingest fresh YouTube Shorts (≤60s, recent) for owner calibration.

Searches recent MLBB Shorts; rejects pre-2024 and stale uploads.
Output: /root/datasets/mlbb/youtube_shorts/ + data/mlbb/youtube_shorts_index.json
"""

from __future__ import annotations

import argparse
import json
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
    ingest_sent_blocklist,
    labeled_ids,
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
    min_year = _min_year(env)
    skip = skip_ids or set()
    hungry = len(skip) > 200 or env.get("MLBB_INGEST_HUNGRY", "0") == "1"
    depth = int(
        env.get(
            "MLBB_INGEST_HUNGRY_SEARCH_DEPTH" if hungry else "MLBB_INGEST_SEARCH_DEPTH",
            "40" if hungry else "12",
        )
    )
    search_n = max(limit * depth, 800 if hungry else 120)
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
        if vid in skip:
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
        if _ffprobe_duration(dest) <= 0:
            dest.unlink(missing_ok=True)
            return None
        return dest
    for alt in out_dir.glob(f"yt_{video_id}.*"):
        if alt.suffix.lower() in (".mp4", ".mkv", ".webm") and alt.stat().st_size > 50_000:
            if _ffprobe_duration(alt) <= 0:
                alt.unlink(missing_ok=True)
                continue
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


def _date_ok(ud: str, env: dict[str, str], days: int) -> bool:
    if not ud.isdigit() or len(ud) < 8:
        return False
    min_year = int(env.get("MLBB_SHORTS_MIN_YEAR", "2024"))
    if int(ud[:4]) < min_year:
        return False
    if env.get("MLBB_SHORTS_YEAR_ONLY", "1") == "1":
        return True
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
    return ud >= cutoff


def _resolve_upload_date(row: dict, env: dict[str, str]) -> str:
    ud = str(row.get("upload_date") or "").strip()
    if ud in ("", "NA", "N/A") or not ud.isdigit():
        ud = fetch_upload_date(str(row.get("video_id", "")), env)
        if ud:
            row["upload_date"] = ud
    return ud


def _hero_search_queries() -> list[str]:
    queries: list[str] = []
    repo = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
    heroes_path = repo / "config" / "mlbb_heroes.json"
    if not heroes_path.exists():
        return queries
    try:
        data = json.loads(heroes_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return queries
    for hero in data.get("heroes", []):
        tags = hero.get("tags") or []
        if tags:
            queries.append(f"mlbb {tags[0]} savage shorts 2025")
    return queries


def _ordered_search_queries(start_slot: int, *, hungry: bool) -> list[str]:
    queries = list(SEARCH_QUERIES)
    if hungry:
        queries.extend(_hero_search_queries())
    return queries[start_slot:] + queries[:start_slot]


def _sweep_pool(
    env: dict[str, str],
    *,
    days: int,
    known: dict[str, str],
    already_sent: set[str],
    sent_pending: set[str],
    max_per_query: int,
    search_delay: float,
    start_slot: int,
    max_queries: int,
) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    blocklist = set(known) | already_sent | sent_pending
    hungry = len(blocklist) > 200 or env.get("MLBB_INGEST_HUNGRY", "0") == "1"
    ordered = _ordered_search_queries(start_slot, hungry=hungry)
    query_cap = min(max_queries, len(ordered)) if hungry else max_queries
    if hungry:
        query_cap = min(query_cap, int(env.get("MLBB_INGEST_HUNGRY_MAX_QUERIES", "8")))
    for query in ordered[:query_cap]:
        blocklist |= seen
        for row in search_shorts(
            query,
            limit=max_per_query,
            env=env,
            days=days,
            skip_ids=blocklist,
        ):
            vid = row["video_id"]
            if vid in seen or vid in known or vid in already_sent or vid in sent_pending:
                continue
            if hungry:
                seen.add(vid)
                out.append(row)
                continue
            ud = _resolve_upload_date(row, env)
            if not _date_ok(ud, env, days):
                continue
            seen.add(vid)
            out.append(row)
        if len(out) >= max_per_query * 2:
            break
        if search_delay > 0:
            time.sleep(search_delay)
    out.sort(key=_sort_freshness_key, reverse=True)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-per-query", type=int, default=30)
    parser.add_argument("--days", type=int, default=int(os.environ.get("MLBB_SHORTS_DAYS", "365")))
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
    slot = int(time.time() // 120) % len(queries)
    if args.incremental:
        hungry = pending_n < int(env.get("MLBB_TARGET_PENDING", "25"))
        if hungry:
            env["MLBB_INGEST_HUNGRY"] = "1"
            ordered = _ordered_search_queries(slot, hungry=True)
            print(
                f"incremental sweep pending={pending_n} days={args.days} "
                f"queries={len(ordered)} start={ordered[0]}"
            )
            pool = _sweep_pool(
                env,
                days=args.days,
                known=labeled_ids(),
                already_sent=ingest_sent_blocklist(),
                sent_pending={str(r.get("video_id", "")) for r in pending_candidates(limit=9999)},
                max_per_query=max(args.max_per_query, 12),
                search_delay=args.search_delay,
                start_slot=slot,
                max_queries=min(len(ordered), int(env.get("MLBB_INGEST_HUNGRY_MAX_QUERIES", "8"))),
            )
            pool = pool[: args.max_per_query * 4]
            queries = []
        else:
            queries = [queries[slot]]
            print(f"incremental query={queries[0]} pending={pending_n} days={args.days}")

    known = labeled_ids()
    already_sent = ingest_sent_blocklist()
    sent_pending = {str(r.get("video_id", "")) for r in pending_candidates(limit=9999)}

    if not queries:
        cap = args.max_per_query * 6
    else:
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
        pool = pool[: cap * 3]

        fresh_pool: list[dict] = []
        for row in pool:
            vid = row["video_id"]
            if vid in known or vid in already_sent or vid in sent_pending:
                continue
            fresh_pool.append(row)
        pool = fresh_pool[:cap]

        if not pool and args.incremental:
            pool = _sweep_pool(
                env,
                days=args.days,
                known=known,
                already_sent=already_sent,
                sent_pending=sent_pending,
                max_per_query=max(args.max_per_query, 12),
                search_delay=args.search_delay,
                start_slot=slot,
                max_queries=10,
            )

    saved = rejected = downloads = skipped_known = 0
    lenient = os.environ.get("MLBB_CALIBRATION_LENIENT", "1") == "1"
    fast_ingest = os.environ.get("MLBB_CALIBRATION_FAST_INGEST", "0") == "1" or env.get(
        "MLBB_INGEST_HUNGRY", "0"
    ) == "1"
    for row in pool:
        if args.max_downloads > 0 and downloads >= args.max_downloads:
            break
        vid = row["video_id"]
        if vid in known or vid in already_sent:
            skipped_known += 1
            continue
        mp4 = SHORTS_ROOT / f"yt_{vid}.mp4"
        if not mp4.exists() and not args.skip_download:
            ud = str(row.get("upload_date") or "").strip()
            if ud in ("", "NA", "N/A") or not ud.isdigit():
                ud = _resolve_upload_date(row, env)
            if ud and not _date_ok(ud, env, args.days):
                rejected += 1
                continue
            mp4 = download_short(row["url"], SHORTS_ROOT, env, vid, days=args.days) or mp4
            downloads += 1
            time.sleep(max(2.0, args.download_delay))
        if not mp4.exists() or mp4.name != f"yt_{vid}.mp4":
            rejected += 1
            continue

        hungry_mode = env.get("MLBB_INGEST_HUNGRY", "0") == "1"
        dur = _ffprobe_duration(mp4)
        if hungry_mode and lenient and dur >= 3.0:
            ok, gscore, reason = is_mlbb_calibration_short(mp4, description=row.get("title", ""))
            hard_reject = reason in ("promo_text", "csv_lookup", "other_game_title", "promo_edit")
            if hard_reject or not ok:
                rejected += 1
                continue
            feats = light_clip_features(mp4)
            feats["score"] = max(float(feats.get("score") or 0), float(gscore))
            upsert_candidate(
                {
                    **row,
                    **feats,
                    "path": str(mp4),
                    "gameplay_pass": 1,
                    "gameplay_score": round(float(gscore), 4),
                    "gameplay_reason": reason,
                    "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            saved += 1
            print(f"OK {vid} score={feats['score']:.3f} hungry=1 views={row.get('view_count')} {row.get('title','')[:50]}")
            continue

        ok, gscore, reason = is_mlbb_calibration_short(mp4, description=row.get("title", ""))
        hard_reject = reason in ("promo_text", "csv_lookup", "other_game_title", "promo_edit")
        if hard_reject or not ok:
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
