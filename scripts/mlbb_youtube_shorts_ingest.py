#!/usr/bin/env python3
"""
MLBB-only: ingest top YouTube Shorts (≤60s, ~90 days) for owner calibration.

Searches: mobile legends highlights, mlbb teamfight, mlbb savage
Filters: gameplay_gate, highlight_scorer (mobile_legends)
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

from gameplay_gate import is_gameplay_video
from highlight_scorer import WINDOW_SEC, score_candidate_window
from mlbb_calibration_store import SHORTS_ROOT, upsert_candidate
from viral_scorer import hook_score
from youtube_download import load_env, ytdlp_cmd, ytdlp_extra_args

SEARCH_QUERIES = (
    "mobile legends highlights",
    "mlbb teamfight",
    "mlbb savage",
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


def search_shorts(query: str, *, limit: int, env: dict[str, str], days: int) -> list[dict]:
    import subprocess

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
    search_n = max(limit * 5, 50)
    cmd = ytdlp_cmd(env, use_proxy=False) + [
        f"ytsearch{search_n}:{query} #shorts",
        "--flat-playlist",
        "--print",
        "%(id)s\t%(title)s\t%(view_count)s\t%(duration)s\t%(upload_date)s\t%(webpage_url)s",
        "--no-download",
        *ytdlp_extra_args(env),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=180)
    entries: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        vid, title, views, dur, upload_date, url = parts[:6]
        if not vid or len(vid) != 11:
            continue
        if upload_date and upload_date.isdigit() and upload_date < cutoff:
            continue
        try:
            duration = float(dur or 0)
            view_count = int(float(views or 0))
        except (ValueError, TypeError):
            continue
        if duration <= 3 or duration > 60:
            continue
        if NEGATIVE_TITLE.search(title):
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
        env.get("YOUTUBE_SHORTS_FORMAT", "bv*[height<=1080]+ba/b[height<=720]/b"),
        "--merge-output-format",
        "mp4",
        "-o",
        template,
        "--no-playlist",
        *ytdlp_extra_args(env),
        url,
    ]
    dest = out_dir / f"yt_{video_id}.mp4"
    if dest.exists():
        return dest
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)
    if proc.returncode != 0:
        return None
    if dest.exists():
        return dest
    mp4s = sorted(out_dir.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return mp4s[0] if mp4s else None


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
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--min-score", type=float, default=0.12)
    args = parser.parse_args()

    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("CONTENT_BOT_REPO", "/root/content_bot_ml")
    env = {**os.environ, **load_env()}
    SHORTS_ROOT.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    pool: list[dict] = []
    for query in SEARCH_QUERIES:
        for row in search_shorts(query, limit=args.max_per_query, env=env, days=args.days):
            vid = row["video_id"]
            if vid in seen:
                continue
            seen.add(vid)
            pool.append(row)

    pool.sort(key=lambda r: int(r.get("view_count") or 0), reverse=True)
    pool = pool[: args.max_per_query * len(SEARCH_QUERIES)]

    saved = rejected = 0
    for row in pool:
        vid = row["video_id"]
        mp4 = SHORTS_ROOT / f"yt_{vid}.mp4"
        if not mp4.exists() and not args.skip_download:
            mp4 = download_short(row["url"], SHORTS_ROOT, env, vid) or mp4
            time.sleep(0.5)
        if not mp4.exists():
            continue

        ok, gscore, reason = is_gameplay_video(mp4, csv_lookup={}, description=row.get("title", ""))
        if not ok:
            rejected += 1
            continue

        feats = score_clip(mp4)
        if feats["score"] < args.min_score and not feats["rule_pass"]:
            rejected += 1
            continue

        upsert_candidate(
            {
                **row,
                **feats,
                "path": str(mp4),
                "gameplay_score": round(float(gscore), 4),
                "gameplay_reason": reason,
                "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        saved += 1
        print(f"OK {vid} score={feats['score']:.3f} views={row.get('view_count')} {row.get('title','')[:50]}")

    print(f"SUMMARY saved={saved} rejected={rejected} pool={len(pool)} dir={SHORTS_ROOT}")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
