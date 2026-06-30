#!/usr/bin/env python3
"""Audit inbox VODs per game — highlight pool vs post-gate blockers (banner / combat / peak gap)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from strict_montage_direct import discover_strict_candidates, file_sha256
from vod_game_registry import VOD_GAMES, adaptive_streak_fn, is_extended_game, load_state, soften_level_fn, spec
from vod_peak_gap import (
    filter_blocked_peaks,
    load_index_segments,
    segment_gap_sec,
    used_peak_times_shooter,
)


def _ffprobe_duration(vod: Path) -> float:
    from mlbb_vod_segment_feed import _ffprobe_duration as fn

    return fn(vod)


def _load_feed_sent(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(x) for x in data.get("sent", [])}
    except (json.JSONDecodeError, OSError):
        return set()


@contextmanager
def _soften_env(game: str, level: int) -> Iterator[None]:
    if level <= 0:
        yield
        return
    if is_extended_game(game):
        from extended_vod_adaptive_gate import overrides_for_level

        overrides = overrides_for_level(game, level)
    else:
        from shooter_vod_adaptive_gate import overrides_for_level

        overrides = overrides_for_level(level)
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        os.environ.update(overrides)
        yield
    finally:
        for key, prev in saved.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev


def audit_mlbb_vod(vod: Path, *, soften_level: int = 0) -> dict:
    from audit_mlbb_vod_inbox import audit_vod

    return audit_vod(vod, soften_level=soften_level)


def audit_peak_vod(game: str, vod: Path, *, soften_level: int = 0) -> dict:
    s = spec(game)
    profile = s.profile
    vid = vod.stem.replace("yt_", "")[:11]
    sent_set = _load_feed_sent(s.feed_sent_path())
    labeled: set[str] = set()
    labels_path = s.labels_path()
    if labels_path.exists():
        try:
            labels = json.loads(labels_path.read_text(encoding="utf-8"))
            for bucket in ("good", "bad", "feedback"):
                for row in labels.get(bucket, []):
                    sid = str(row.get("segment_id", ""))
                    if sid:
                        labeled.add(sid)
        except (json.JSONDecodeError, OSError):
            pass

    state = load_state(game)
    level = soften_level if soften_level > 0 else soften_level_fn(game)(adaptive_streak_fn(game)(state))

    report: dict = {
        "game": game,
        "vod": vod.name,
        "vid": vid,
        "duration_sec": round(_ffprobe_duration(vod), 1),
        "soften_level": level,
        "pool_size": 0,
        "pool_peaks": [],
        "available_peaks": [],
        "blocked_peaks": [],
        "used_sent_peaks": [],
        "gap_sec": segment_gap_sec(game, soften_level=level),
        "metro_ok": None,
        "presend_sample": None,
    }

    if game == "pubg":
        from pubg_metro_royale_gate import vod_looks_metro_royale

        ok, reason = vod_looks_metro_royale(vod)
        report["metro_ok"] = ok
        report["metro_reason"] = reason

    sig = file_sha256(vod)
    with _soften_env(game, level):
        pool = discover_strict_candidates(vod, profile, sig, labeled | sent_set)

    report["pool_size"] = len(pool)
    pool_peaks = [float(c.get("start", 0)) for c in pool[:24]]
    report["pool_peaks"] = [round(p, 1) for p in pool_peaks[:8]]

    index = load_index_segments(s.index_path())
    used = used_peak_times_shooter(vid, sent_set, index)
    report["used_sent_peaks"] = used
    available, blocked = filter_blocked_peaks(pool_peaks, used, gap_sec=report["gap_sec"])
    report["available_peaks"] = [round(p, 1) for p in available[:8]]
    report["blocked_peaks"] = [round(p, 1) for p in blocked[:8]]

    if available and pool:
        start = max(0.0, available[0] - float(os.environ.get("MLBB_VOD_LEAD_SEC", "4")))
        from strict_segment_gate import passes_strict_gate

        ok, reason, _ = passes_strict_gate(vod, start, 15.0, profile)
        report["presend_sample"] = {"peak": available[0], "ok": ok, "reason": reason}

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit VOD inbox per game")
    parser.add_argument("--game", default="all", choices=("all", *VOD_GAMES))
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--soften", type=int, default=-1, help="Force soften level (-1=from state)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    games = list(VOD_GAMES) if args.game == "all" else [args.game]
    rows: list[dict] = []

    for game in games:
        inbox = spec(game).inbox()
        if not inbox.is_dir():
            print(f"{game}: inbox missing {inbox}", file=sys.stderr)
            continue
        vods = sorted(inbox.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)[: args.limit]
        state = load_state(game)
        soften = (
            args.soften
            if args.soften >= 0
            else soften_level_fn(game)(adaptive_streak_fn(game)(state))
        )
        for vod in vods:
            if game == "mlbb":
                rows.append(audit_mlbb_vod(vod, soften_level=soften))
            else:
                rows.append(audit_peak_vod(game, vod, soften_level=soften))

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    for row in rows:
        if row.get("game") == "mlbb" or "pass_peaks" in row:
            print(
                f"MLBB {row['vod']}: pass={row.get('pass_peaks', 0)} "
                f"motion={row.get('motion_ok', 0)} banner_reject={row.get('banner_reject', 0)} "
                f"L{row.get('active_level', row.get('soften_level', 0))}"
            )
        else:
            ps = row.get("presend_sample") or {}
            print(
                f"{row['game'].upper()} {row['vod']}: pool={row['pool_size']} "
                f"avail={len(row.get('available_peaks', []))} blocked={len(row.get('blocked_peaks', []))} "
                f"gap={row.get('gap_sec')} L{row.get('soften_level', 0)} "
                f"presend={ps.get('ok')} {str(ps.get('reason', ''))[:40]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
