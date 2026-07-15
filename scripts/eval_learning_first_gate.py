#!/usr/bin/env python3
"""LEARNING_FIRST transition gate — run before enabling sendVideo."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_learning_first import (
    enabled,
    eval_transition_gate,
    precision_7d,
    sends_allowed,
    transition_passed,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="MLBB LEARNING_FIRST gate eval")
    parser.add_argument("--require-pass", action="store_true", help="exit 1 if gate not passed")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--holdout-min", type=float, default=0.85)
    parser.add_argument("--dry-min-rejected", type=int, default=7)
    args = parser.parse_args()

    os.environ.setdefault("HIGHLIGHT_OWNER_BAD_PAD_SEC", "90")
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")

    report = eval_transition_gate(
        holdout_min_precision=args.holdout_min,
        dry_min_rejected=args.dry_min_rejected,
    )

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"MLBB_LEARNING_FIRST={enabled()} transition={transition_passed()} sends_allowed={sends_allowed()}")
        print(f"precision_7d={precision_7d():.0%}")
        print("")
        bb = report["bad_block"]
        print(f"1) bad_block ±90s: {'PASS' if bb['pass'] else 'FAIL'} ({len(bb.get('cases', []))} cases)")
        for case in bb.get("cases", []):
            mark = "OK" if case.get("ok") else "FAIL"
            print(f"   {mark} {case.get('segment_id')} overlap={case.get('overlap')} blocked={case.get('blocked_starts')}")
        ho = report["holdout"]
        print(f"2) holdout precision: {ho.get('precision', 0):.0%} ({'PASS' if ho.get('pass') else 'FAIL'}) "
              f"eval={ho.get('evaluated')} good_pass={ho.get('good_pass')} bad_fp={ho.get('bad_false_pass')}")
        dr = report["dry_run"]
        print(f"3) dry-run gates: rejected {dr.get('rejected')}/{dr.get('tested')} ({'PASS' if dr.get('pass') else 'FAIL'})")
        print("")
        print(f"ALL_PASS={report['all_pass']}")

    if args.require_pass and not report["all_pass"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
