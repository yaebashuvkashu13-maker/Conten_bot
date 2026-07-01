#!/usr/bin/env python3
"""Re-queue inbox VODs wrongly marked exhausted after gate fixes — all VOD games."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vod_game_registry import VOD_GAMES, inbox_video_ids, load_state, save_state, spec


def _gate_reject_match(game: str, reason: str) -> bool:
    r = reason.lower()
    if game == "pubg":
        return "metro" in r or "not_metro" in r
    if game == "mlbb":
        return "banner" in r or "motion" in r
    if game == "genshin":
        return any(k in r for k in ("boss", "gate", "weak_boss", "low_boss"))
    if game == "wot":
        return any(k in r for k in ("impact", "hit", "cruise", "drive", "burst"))
    return bool(r) and r != "none"


def reset_game(
    game: str,
    *,
    dry_run: bool = False,
    gate_reject_only: bool = False,
    clear_reject_reason: bool = True,
) -> int:
    s = spec(game)
    state_path = s.state_path()
    if not state_path.exists():
        print(f"{game}: state missing {state_path}")
        return 0

    state = load_state(game)
    inbox_ids = inbox_video_ids(game)
    reset = 0

    for row in state.get("vods") or []:
        vid = str(row.get("id") or "")
        if not vid or vid not in inbox_ids:
            continue
        if not row.get("exhausted"):
            continue
        if gate_reject_only and not _gate_reject_match(game, str(row.get("reject_reason") or "")):
            continue
        if dry_run:
            print(f"  would reset {vid} reason={str(row.get('reject_reason', ''))[:60]}")
        else:
            row["exhausted"] = False
            if clear_reject_reason:
                row.pop("reject_reason", None)
        reset += 1

    if not dry_run and reset:
        save_state(game, state)

    print(f"{game}: {'would reset' if dry_run else 'reset'} {reset} inbox VODs (inbox={len(inbox_ids)})")
    return reset


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset exhausted flag for inbox VODs (all VOD games)")
    parser.add_argument(
        "--game",
        default="all",
        choices=("all", *VOD_GAMES),
        help="Game to reset (default: all)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--gate-reject-only",
        action="store_true",
        help="Only reset VODs rejected by discovery/gate",
    )
    args = parser.parse_args()

    games = list(VOD_GAMES) if args.game == "all" else [args.game]
    total = 0
    for game in games:
        total += reset_game(
            game,
            dry_run=args.dry_run,
            gate_reject_only=args.gate_reject_only,
        )
    print(f"total reset={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
