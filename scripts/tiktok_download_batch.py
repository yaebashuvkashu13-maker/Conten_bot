#!/usr/bin/env python3
"""Download MLBB TikTok clips via proxy for training (gameplay-only)."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from gameplay_gate import extract_video_id, is_gameplay_video, load_csv_lookup

STATE_PATH = Path("/root/data/mlbb/download_state.json")
RANKED_CSV = Path("/root/data/mlbb/current_mlbb_ranked_videos.csv")
GAMEPLAY_CSV = Path("/root/data/mlbb/gameplay_filter_latest.csv")
DEFAULT_OUT = Path("/root/datasets/tiktok/mlbb")


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"downloaded_ids": [], "rejected_ids": [], "last_run": None}
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def iter_ranked_rows(csv_path: Path, limit: int) -> list[dict]:
    rows: list[dict] = []
    with csv_path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            url = (row.get("webpage_url") or "").strip()
            if not url or "tiktok.com" not in url:
                continue
            rows.append(row)
    rows.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
    return rows[:limit]


def output_path_for(row: dict, out_root: Path) -> Path:
    label = (row.get("source_label") or "unknown").replace("/", "-")
    video_id = row.get("video_id") or extract_video_id(Path(label), row.get("description", ""))
    return out_root / label / f"{video_id}.mp4"


def download_one(url: str, dest: Path, proxy: str) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 100_000:
        return True
    partial = dest.with_suffix(".mp4.part")
    cmd = [
        "yt-dlp",
        "--proxy",
        proxy,
        "--no-playlist",
        "--merge-output-format",
        "mp4",
        "-o",
        str(partial),
        url,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"download failed {url}: {exc}", file=sys.stderr)
        if partial.exists():
            partial.unlink(missing_ok=True)
        return False
    if partial.exists():
        partial.replace(dest)
    return dest.exists() and dest.stat().st_size > 100_000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--csv", type=Path, default=RANKED_CSV)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--env", type=Path, default=Path("/root/.video_bot.env"))
    args = parser.parse_args()

    env = load_env(args.env)
    proxy = env.get("YTDLP_PROXY") or env.get("SOCKS5_PROXY") or env.get("PROXY_URL")
    if not proxy:
        print("ERROR: PROXY_URL missing in env", file=sys.stderr)
        return 1
    if not args.csv.exists():
        print(f"ERROR: ranked csv missing: {args.csv}", file=sys.stderr)
        return 1

    state = load_state()
    downloaded_ids = set(state.get("downloaded_ids", []))
    rejected_ids = set(state.get("rejected_ids", []))
    lookup = load_csv_lookup(GAMEPLAY_CSV)

    rows = iter_ranked_rows(args.csv, limit=max(args.limit * 3, args.limit))
    stats = {"attempted": 0, "downloaded": 0, "gameplay_kept": 0, "rejected": 0, "skipped": 0}

    for row in rows:
        if stats["downloaded"] >= args.limit:
            break
        video_id = str(row.get("video_id") or "")
        if video_id in downloaded_ids or video_id in rejected_ids:
            stats["skipped"] += 1
            continue
        dest = output_path_for(row, args.out)
        url = row["webpage_url"]
        stats["attempted"] += 1
        if not download_one(url, dest, proxy):
            continue
        stats["downloaded"] += 1
        ok, score, reason = is_gameplay_video(
            dest,
            csv_lookup=lookup,
            description=row.get("description", ""),
            min_score=0.78,
        )
        if not ok:
            dest.unlink(missing_ok=True)
            rejected_ids.add(video_id)
            stats["rejected"] += 1
            print(f"reject {video_id} score={score:.2f} reason={reason}")
        else:
            downloaded_ids.add(video_id)
            stats["gameplay_kept"] += 1
            print(f"kept {video_id} -> {dest}")
        time.sleep(1)

    state["downloaded_ids"] = sorted(downloaded_ids)[-5000:]
    state["rejected_ids"] = sorted(rejected_ids)[-5000:]
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_stats"] = stats
    save_state(state)
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
