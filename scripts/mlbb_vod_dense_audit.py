#!/usr/bin/env python3
"""Dense 1 Hz banner audit on largest inbox VODs — no frame skips."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_kill_banner import discover_vod_kill_banners  # noqa: E402
from mlbb_vod_title import title_min_banner_tier, vod_title_blob  # noqa: E402


def _ffprobe_duration(vod: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(vod),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    try:
        return float((proc.stdout or "0").strip())
    except ValueError:
        return 0.0


def _inbox_root() -> Path:
    return Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))


def _largest_vods(inbox: Path, limit: int) -> list[Path]:
    files = [p for p in inbox.glob("yt_*.mp4") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    return files[:limit]


def audit_vod(vod: Path) -> dict:
    dur = _ffprobe_duration(vod)
    blob = vod_title_blob(vod)
    title_tier = title_min_banner_tier(blob)
    os.environ["MLBB_VOD_BANNER_DENSE_SEC"] = "1"
    os.environ["MLBB_VOD_BANNER_DISCOVER"] = "1"
    os.environ["MLBB_VOD_BANNER_TIMESTEP_SCAN"] = "1"
    os.environ["MLBB_KILL_BANNER_DISCOVER_STEP"] = "1"
    os.environ["MLBB_KILL_BANNER_DISCOVER_MAX_PROBES"] = str(
        max(600, int(dur) + 64)
    )
    os.environ["MLBB_KILL_BANNER_DISCOVER_MAX_SEC"] = str(
        max(900.0, min(2400.0, dur * 1.5))
    )
    if title_tier > 0:
        os.environ["MLBB_VOD_TITLE_MIN_TIER"] = str(title_tier)

    t0 = time.monotonic()
    hits = discover_vod_kill_banners(vod, min_tier=title_tier if title_tier > 0 else None)
    elapsed = time.monotonic() - t0

    by_tier: dict[str, int] = {}
    rows = []
    for h in hits:
        by_tier[h.label] = by_tier.get(h.label, 0) + 1
        rows.append(
            {
                "sec": round(h.sec, 1),
                "tier": h.tier,
                "label": h.label,
                "text": h.text[:80],
                "source": h.source,
            }
        )

    return {
        "vod": vod.name,
        "size_mb": round(vod.stat().st_size / (1024 * 1024), 1),
        "duration_sec": round(dur, 1),
        "title_blob": blob[:120],
        "title_min_tier": title_tier,
        "scan_sec": round(elapsed, 1),
        "hits": len(hits),
        "by_tier": by_tier,
        "banner_times": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Dense 1 Hz MLBB banner audit")
    parser.add_argument("--inbox", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--vod", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    inbox = args.inbox or _inbox_root()
    vods = [Path(v) for v in args.vod] if args.vod else _largest_vods(inbox, args.limit)
    if not vods:
        print(json.dumps({"error": f"no vods in {inbox}"}, ensure_ascii=False))
        return 1

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "inbox": str(inbox),
        "dense_1hz": True,
        "vods": [],
    }
    for vod in vods:
        if not vod.exists():
            report["vods"].append({"vod": vod.name, "error": "missing"})
            continue
        print(f"auditing {vod.name} ({vod.stat().st_size // (1024*1024)} MB)...", flush=True)
        report["vods"].append(audit_vod(vod))

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
