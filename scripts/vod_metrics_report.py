#!/usr/bin/env python3
"""Aggregate VOD scan funnel metrics from shooter segment state."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load_state(game: str) -> dict:
    from shooter_vod_segment_store import _paths
    from vod_state_io import load_json_state

    p = _paths(game)["state"]
    return load_json_state(p, lambda: {"vods": [], "used_youtube_ids": []})


def report(game: str = "pubg", *, since_hours: float = 24.0) -> dict:
    state = _load_state(game)
    cutoff = time.time() - since_hours * 3600.0
    rows: list[dict] = []
    for entry in state.get("vods") or []:
        if not isinstance(entry, dict):
            continue
        last_at = float(entry.get("last_scan_at") or 0)
        if last_at < cutoff:
            continue
        funnel = entry.get("last_scan_funnel") or {}
        sent = int(entry.get("last_scan_sent") or funnel.get("sent") or 0)
        approved = int(funnel.get("approved") or sent)
        timings = entry.get("last_scan_timings_ms") or funnel.get("timings_ms") or {}
        rows.append(
            {
                "vod_id": entry.get("vod_id") or entry.get("youtube_id") or entry.get("path", "")[-15:],
                "sent": sent,
                "approved": approved,
                "reject_reason": entry.get("reject_reason"),
                "funnel": funnel,
                "timings_ms": timings,
            }
        )
    total_sent = sum(r["sent"] for r in rows)
    total_approved = sum(r["approved"] for r in rows)
    elapsed_min = max(since_hours * 60.0, 1.0)
    reject_reasons: dict[str, int] = {}
    cache_hits = 0
    for row in rows:
        reason = str(row.get("reject_reason") or "")
        if reason:
            reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
        funnel = row.get("funnel") or {}
        if funnel.get("feature_cache_hit") or funnel.get("ranked_pool_cache_hit"):
            cache_hits += 1
    return {
        "game": game,
        "since_hours": since_hours,
        "vods_scanned": len(rows),
        "total_sent": total_sent,
        "total_approved": total_approved,
        "approved_clips_per_min": round(total_approved / elapsed_min, 4),
        "cache_hit_vods": cache_hits,
        "reject_reasons": reject_reasons,
        "rows": rows[:50],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", default="pubg")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = report(args.game, since_hours=args.hours)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
