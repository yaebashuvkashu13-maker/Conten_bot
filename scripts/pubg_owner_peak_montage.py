#!/usr/bin/env python3
"""Force a PUBG VOD montage from owner-specified fight peaks (one-off redo)."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pubg_owner_peak_montage")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_peaks(raw: list[str]) -> list[float]:
    out: list[float] = []
    for item in raw:
        for part in item.split(","):
            part = part.strip()
            if part:
                out.append(float(part))
    return sorted(out)


def _peak_rows(vod: Path, peaks: list[float], sig: str) -> list[dict]:
    rows: list[dict] = []
    for peak in peaks:
        sid = f"owner_{vod.stem}_{int(round(peak))}"
        rows.append(
            {
                "segment_id": sid,
                "source_path": str(vod),
                "game_name": "pubg",
                "start": peak,
                "peak_start": peak,
                "score": 0.92,
                "owner_anchor": True,
                "clip": {"start": peak, "peak_start": peak},
                "source_signature": sig,
            }
        )
    return rows


def _clear_montage_sent(game: str, vod: Path, peaks: list[float]) -> None:
    from shooter_vod_segment_store import _paths, load_feed_sent, mark_feed_sent

    vid = vod.stem[3:] if vod.stem.startswith("yt_") else vod.stem
    peaks_rounded = {int(round(p)) for p in peaks}
    sent = load_feed_sent(game)
    drop = {f"{vid}_{p}" for p in peaks_rounded}
    kept = sent - drop
    if kept != sent:
        mark_feed_sent(game, list(kept))
        log.info("cleared sent keys %s", sorted(drop))


def main() -> int:
    parser = argparse.ArgumentParser(description="Render PUBG montage from owner fight peaks")
    parser.add_argument("--vod", required=True, help="Path or yt_VIDEOID under inbox")
    parser.add_argument(
        "--peaks",
        nargs="+",
        required=True,
        help="Peak seconds (space or comma separated), e.g. 1533 5266",
    )
    parser.add_argument("--clear-sent", action="store_true", help="Drop matching peaks from sent log")
    parser.add_argument("--dry-run", action="store_true", help="Print clip bounds only")
    args = parser.parse_args()

    vod = Path(args.vod)
    if not vod.exists():
        for base in (
            Path("/root/data/pubg/youtube_nightly/inbox"),
            Path("/root/data/mlbb/youtube_nightly/inbox"),
        ):
            candidate = base / (args.vod if args.vod.name.endswith(".mp4") else f"yt_{args.vod}.mp4")
            if candidate.exists():
                vod = candidate
                break
    if not vod.exists():
        print(f"REFUSED vod_missing {args.vod}")
        return 2

    peaks = _parse_peaks(args.peaks)
    from pubg_owner_style import style_avoid_peaks

    avoid = style_avoid_peaks(vod)
    if avoid:
        peaks = [
            peak
            for peak in peaks
            if not any(abs(float(peak) - float(bad)) <= 25.0 for bad in avoid)
        ]
    if len(peaks) < 2:
        from pubg_owner_style import style_reference_peaks

        refs = style_reference_peaks(vod)
        if len(refs) >= 1:
            span = float(os.environ.get("SHOOTER_VOD_MONTAGE_CLUSTER_SPAN_SEC", "240"))
            gap = float(os.environ.get("SHOOTER_VOD_MONTAGE_PART_GAP_SEC", "20"))
            peaks = sorted(set(refs))
            log.info("style_ref_only peaks=%s — add nearby fights in cluster span %.0fs", peaks, span)
        if len(peaks) < 2:
            print("REFUSED need>=2 peaks for PUBG montage (or set style_ref + cluster fights)")
            return 2

    os.environ.setdefault("PUBG_FIGHT_SEGMENTER", "1")
    os.environ.setdefault("SHOOTER_VOD_MONTAGE_MIN_CLIPS", "2")
    os.environ.setdefault("SHOOTER_VOD_MONTAGE_MAX_CLIPS", "2")
    os.environ.setdefault("SHOOTER_VOD_MONTAGE_PREFER_PARTS", "2")

    from shooter_vod_segment_feed import _prepare_montage_clip, _send_montage, _montage_limits
    from pubg_fight_segment import resolve_pubg_fight_bounds

    sig = file_sha256(vod)
    rows = _peak_rows(vod, peaks, sig)

    if args.dry_run:
        from shooter_vod_segment_feed import _ffprobe_duration

        file_dur = _ffprobe_duration(vod)
        _min, _max, _gap, part_max, _final = _montage_limits()
        for row in rows:
            peak = float(row["peak_start"])
            start, dur, report = resolve_pubg_fight_bounds(vod, peak, file_duration=file_dur)
            clip = _prepare_montage_clip(row, vod, part_max=part_max, game="pubg")
            print(
                json.dumps(
                    {
                        "peak": peak,
                        "bounds": {"start": start, "duration": dur, "end": start + dur},
                        "clip": clip,
                        "report": {
                            k: report[k]
                            for k in (
                                "fight_end",
                                "kill_time",
                                "shooting_start",
                                "killfeed_score",
                            )
                            if k in report
                        },
                    },
                    ensure_ascii=False,
                )
            )
        return 0

    token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("PUBG_CHAT_IDS", "").split(",")[0].strip() or os.environ.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("REFUSED missing TG_BOT_TOKEN or PUBG_CHAT_IDS/TG_CHAT_ID")
        return 2

    if args.clear_sent:
        _clear_montage_sent("pubg", vod, peaks)

    log.info("owner peak montage vod=%s peaks=%s", vod.name, peaks)
    sent = _send_montage("pubg", token, chat_id, vod, rows, sig)
    if sent < 1:
        print(f"REFUSED montage_not_sent peaks={peaks}")
        return 1
    print(f"OK montage_sent={sent} peaks={peaks} at={time.strftime('%Y-%m-%d %H:%M:%S')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
