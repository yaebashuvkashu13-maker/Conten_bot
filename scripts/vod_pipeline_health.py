#!/usr/bin/env python3
"""One-shot health report for MLBB / PUBG / Standoff VOD pipelines."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from daily_game_cycle import active_game, enabled, quota_remaining, send_count
from vod_game_registry import DAILY_GAMES, exhausted_summary, load_state, spec, streak_from_state


def health_row(game: str) -> dict:
    s = spec(game)
    state = load_state(game)
    exh = exhausted_summary(game, state)
    sent_path = s.feed_sent_path()
    sent_count_file = 0
    if sent_path.exists():
        try:
            sent_count_file = len(json.loads(sent_path.read_text(encoding="utf-8")).get("sent", []))
        except (json.JSONDecodeError, OSError):
            pass

    row = {
        **exh,
        "feed_sent_total": sent_count_file,
        "daily_sent": send_count(game) if enabled() else None,
        "daily_quota_left": quota_remaining(game) if enabled() else None,
        "actionable_inbox": max(0, exh["inbox"] - exh["exhausted_inbox"]),
    }

    if exh["exhausted_inbox"] > exh["inbox"] * 0.5 and exh["streak"] >= 3:
        row["hint"] = "run reset_vod_inbox_exhausted.py --game " + game
    elif exh["streak"] >= 6 and row["actionable_inbox"] > 0:
        row["hint"] = "adaptive soften should be active — check audit_vod_inbox.py"
    elif row["actionable_inbox"] == 0 and exh["inbox"] > 0:
        row["hint"] = "all inbox exhausted — reset after gate fix"
    else:
        row["hint"] = "ok"
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="VOD pipeline health for all daily-cycle games")
    parser.add_argument("--game", default="all", choices=("all", *DAILY_GAMES))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    games = list(DAILY_GAMES) if args.game == "all" else [args.game]
    rows = [health_row(g) for g in games]

    payload = {
        "daily_cycle_enabled": enabled(),
        "active_game": active_game() if enabled() else None,
        "games": rows,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"Daily cycle: {'on' if enabled() else 'off'} | active={payload['active_game']}")
    for row in rows:
        print(
            f"{row['game'].upper()}: inbox={row['inbox']} actionable={row['actionable_inbox']} "
            f"exhausted_inbox={row['exhausted_inbox']} streak={row['streak']} "
            f"daily={row.get('daily_sent')}/{row.get('daily_quota_left')} left "
            f"| {row.get('hint', '')}"
        )
        for reason, cnt in (row.get("top_reject_reasons") or {}).items():
            if cnt >= 2:
                print(f"    reject×{cnt}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
