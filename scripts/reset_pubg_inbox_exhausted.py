#!/usr/bin/env python3
"""Re-queue PUBG inbox VODs wrongly marked exhausted (0 sends) after gate fixes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

STATE_PATH = Path("/root/data/pubg/vod_segment_state.json")
INBOX = Path("/root/data/pubg/youtube_nightly/inbox")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset exhausted flag for PUBG inbox VODs with 0 sends")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state", default=str(STATE_PATH))
    parser.add_argument("--inbox", default=str(INBOX))
    parser.add_argument("--metro-reject-only", action="store_true", help="Only reset metro reject_reason")
    args = parser.parse_args()

    state_path = Path(args.state)
    inbox = Path(args.inbox)
    if not state_path.exists():
        print(f"state missing: {state_path}", file=sys.stderr)
        return 1

    state = json.loads(state_path.read_text(encoding="utf-8"))
    inbox_ids = {p.stem.replace("yt_", "") for p in inbox.glob("yt_*.mp4")}
    zero_ids = {
        str(row.get("id") or "")
        for row in state.get("vod_outcomes") or []
        if int(row.get("sent", 0)) == 0 and row.get("id")
    }

    reset = 0
    for row in state.get("vods") or []:
        vid = str(row.get("id") or "")
        if not vid or vid not in inbox_ids:
            continue
        if not row.get("exhausted"):
            continue
        if args.metro_reject_only:
            reason = str(row.get("reject_reason") or "")
            if "metro" not in reason.lower():
                continue
        if vid in zero_ids or not args.metro_reject_only:
            if args.dry_run:
                print(f"would reset {vid} reason={row.get('reject_reason', '')[:60]}")
            else:
                row["exhausted"] = False
                row.pop("reject_reason", None)
            reset += 1

    if not args.dry_run and reset:
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"{'would reset' if args.dry_run else 'reset'} {reset} inbox VODs (inbox={len(inbox_ids)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
