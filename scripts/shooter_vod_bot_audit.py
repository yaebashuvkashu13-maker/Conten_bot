#!/usr/bin/env python3
"""
Audit PUBG VODs the bot scanned but did not send.

Reads vod_segment_state.json + inbox files. No owner labels — only bot outcomes.

Usage:
  python3 shooter_vod_bot_audit.py
  python3 shooter_vod_bot_audit.py --reopen   # clear wrong fast-skip exhaust flags
  python3 shooter_vod_bot_audit.py VJppl55fJVA Lt4tEJC1EHs
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shooter_vod_fast_scan import vod_fast_combat_check

STATE_PATH = Path(os.environ.get("PUBG_VOD_STATE", "/root/data/pubg/vod_segment_state.json"))
INBOX = Path(os.environ.get("PUBG_VOD_INBOX", "/root/data/pubg/youtube_nightly/inbox"))
TOP_RE = re.compile(r"top=([0-9.]+)")


def _vod_path(vid: str) -> Path | None:
    p = INBOX / f"yt_{vid}.mp4"
    return p if p.exists() and p.stat().st_size > 50_000 else None


def _fast_top(reason: str) -> float:
    m = TOP_RE.search(reason or "")
    return float(m.group(1)) if m else 0.0


def _wrong_fast_exhaust(entry: dict) -> bool:
    reason = str(entry.get("reject_reason") or "")
    if not entry.get("exhausted") or not reason.startswith("fast_panns"):
        return False
    top = _fast_top(reason)
    strong = float(os.environ.get("SHOOTER_VOD_FAST_STRONG_PANN", "0.40"))
    weak = float(os.environ.get("SHOOTER_VOD_FAST_WEAK_PASS_MIN", "0.18"))
    if "min_hits" in reason and top >= strong:
        return True
    if "min_hits" in reason and top >= weak:
        return True
    return False


def audit_entry(entry: dict) -> dict:
    vid = str(entry.get("id") or "")
    vod = _vod_path(vid)
    out: dict = {
        "id": vid,
        "title": (entry.get("title") or "")[:80],
        "exhausted": bool(entry.get("exhausted")),
        "reject_reason": entry.get("reject_reason"),
        "last_pool_peaks": entry.get("last_pool_peaks"),
        "last_scan_sent": entry.get("last_scan_sent"),
        "presend_streak": entry.get("presend_reject_streak"),
    }
    if not vod:
        out["status"] = "missing_mp4"
        return out
    ok, reason, peaks = vod_fast_combat_check(vod, "pubg")
    out["fast_now"] = {"ok": ok, "reason": reason, "peaks": peaks[:6]}
    out["wrong_fast_exhaust"] = _wrong_fast_exhaust(entry)
    if int(entry.get("last_scan_sent") or 0) > 0:
        out["bot_issue"] = "sent_ok"
    elif out["wrong_fast_exhaust"]:
        out["bot_issue"] = "wrong_fast_skip_exhaust"
    elif str(entry.get("reject_reason") or "").startswith("fast_panns_0"):
        out["bot_issue"] = "dead_vod_ok"
    elif entry.get("last_pool_peaks"):
        out["bot_issue"] = "had_peaks_no_send"
    elif str(entry.get("reject_reason") or "") in {"no_combat_peaks", "scan_timeout"}:
        out["bot_issue"] = "scan_empty_or_timeout"
    elif not entry.get("exhausted") and entry.get("last_scan_at"):
        out["bot_issue"] = "scanned_zero_rescan_pending"
    else:
        out["bot_issue"] = "unknown"
    return out


def reopen_wrong_fast_skips(state: dict) -> list[str]:
    reopened: list[str] = []
    for entry in state.get("vods", []):
        if not _wrong_fast_exhaust(entry):
            continue
        entry["exhausted"] = False
        entry.pop("reject_reason", None)
        entry["last_scan_at"] = 0
        reopened.append(str(entry.get("id")))
    return reopened


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit bot-scanned PUBG VODs with 0 sends")
    ap.add_argument("video_ids", nargs="*", help="Optional specific IDs")
    ap.add_argument("--reopen", action="store_true", help="Re-open wrongly fast-skipped VODs")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=15)
    args = ap.parse_args()

    if not STATE_PATH.exists():
        print(f"no state: {STATE_PATH}")
        return 1
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    vods = state.get("vods", [])
    if args.video_ids:
        wanted = set(args.video_ids)
        entries = [v for v in vods if v.get("id") in wanted]
    else:
        entries = sorted(
            [v for v in vods if int(v.get("last_scan_sent") or 0) == 0 and v.get("last_scan_at")],
            key=lambda v: float(v.get("last_scan_at") or 0),
            reverse=True,
        )[: args.limit]

    reports = [audit_entry(e) for e in entries]
    if args.reopen:
        reopened = reopen_wrong_fast_skips(state)
        STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        if args.json:
            print(json.dumps({"reopened": reopened, "reports": reports}, ensure_ascii=False, indent=2))
        else:
            print("reopened:", reopened)
            for r in reports:
                if r.get("id") in reopened:
                    print(f"  ✓ {r['id']} — was wrong fast-skip")
        return 0

    if args.json:
        print(json.dumps(reports, indent=2, ensure_ascii=False))
        return 0

    print(f"streak={state.get('zero_cut_streak')} level={state.get('last_adaptive_level')}")
    for r in reports:
        print(f"\n{r['id']} | {r.get('bot_issue')} | exh={r.get('exhausted')}")
        print(f"  reason={r.get('reject_reason')} peaks={r.get('last_pool_peaks')}")
        fn = r.get("fast_now") or {}
        print(f"  fast_now: ok={fn.get('ok')} {fn.get('reason')}")
        if r.get("wrong_fast_exhaust"):
            print("  ⚠ wrong fast-skip — should rescan with --reopen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
