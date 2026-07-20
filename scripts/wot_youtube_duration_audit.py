#!/usr/bin/env python3
"""Audit WoT YouTube search results — duration distribution + query quality."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from youtube_download import load_env, ytdlp_cmd
from youtube_extended_vod_prefs import _queries_for, rank_wot_vod_candidate, title_ok


def _sample_queries(env: dict, queries: list[str], *, limit: int = 12) -> list[dict]:
    rows: list[dict] = []
    for q in queries:
        cmd = ytdlp_cmd(env) + [
            "--flat-playlist",
            "--print",
            "%(id)s|%(duration)s|%(title)s|%(upload_date)s",
            f"ytsearch{limit}:{q}",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
        for line in (proc.stdout or "").splitlines():
            parts = line.split("|", 3)
            if len(parts) < 3:
                continue
            vid, dur_s, title = parts[0], parts[1], parts[2]
            upload = parts[3] if len(parts) > 3 else ""
            try:
                dur = float(dur_s)
            except ValueError:
                continue
            meta = {
                "id": vid,
                "duration": dur,
                "title": title,
                "upload_date": upload,
                "query": q,
            }
            meta["title_ok"] = title_ok("wot", title)
            meta["rank_score"] = rank_wot_vod_candidate(meta)
            rows.append(meta)
    return rows


def _summarize(rows: list[dict]) -> dict:
    durs = [float(r["duration"]) for r in rows if float(r.get("duration") or 0) > 0]
    buckets = {"<4m": 0, "4-10m": 0, "10-20m": 0, "20-60m": 0, ">60m": 0}
    for x in durs:
        m = x / 60.0
        if m < 4:
            buckets["<4m"] += 1
        elif m < 10:
            buckets["4-10m"] += 1
        elif m < 20:
            buckets["10-20m"] += 1
        elif m < 60:
            buckets["20-60m"] += 1
        else:
            buckets[">60m"] += 1
    ok = [r for r in rows if r.get("title_ok")]
    return {
        "sample_count": len(rows),
        "title_ok_count": len(ok),
        "duration_min_min": round(min(durs) / 60, 1) if durs else 0,
        "duration_max_min": round(max(durs) / 60, 1) if durs else 0,
        "duration_avg_min": round(sum(durs) / len(durs) / 60, 1) if durs else 0,
        "duration_median_min": round(statistics.median(durs) / 60, 1) if durs else 0,
        "duration_p25_min": round(statistics.quantiles(durs, n=4)[0] / 60, 1) if len(durs) >= 4 else None,
        "duration_p75_min": round(statistics.quantiles(durs, n=4)[2] / 60, 1) if len(durs) >= 4 else None,
        "buckets": buckets,
        "recommended_wot_vod_min_sec": 120,
        "recommended_wot_vod_max_sec": 1500,
        "recommended_target_sec": 390,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="WoT YouTube duration audit")
    parser.add_argument("--env", default="/root/.video_bot.env")
    parser.add_argument("--out", default="")
    parser.add_argument("--query-limit", type=int, default=10)
    parser.add_argument("--max-queries", type=int, default=12)
    args = parser.parse_args()

    env = load_env(Path(args.env))
    queries = list(_queries_for("wot"))[: max(1, args.max_queries)]
    rows = _sample_queries(env, queries, limit=max(5, args.query_limit))
    summary = _summarize(rows)
    top = sorted(rows, key=lambda r: float(r.get("rank_score", -999)), reverse=True)[:15]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "queries_used": queries,
        "summary": summary,
        "top_ranked": top,
        "rows": rows,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
