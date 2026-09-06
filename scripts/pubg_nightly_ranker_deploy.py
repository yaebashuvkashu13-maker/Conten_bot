#!/usr/bin/env python3
"""Nightly ranker train → benchmark → champion/challenger promote (manual gate)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("/root/data/pubg/nightly_ranker_report.json"))
    args = parser.parse_args()

    scripts = Path(__file__).resolve().parent
    report: dict = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    candidate = Path(os.environ.get("PUBG_RANKER_MODEL", "/root/data/pubg/pubg_moment_ranker.joblib"))

    if args.train:
        proc = subprocess.run(
            [sys.executable, str(scripts / "pubg_moment_ranker.py"), "train"],
            capture_output=True,
            text=True,
        )
        report["train_rc"] = proc.returncode
        report["train_stdout"] = (proc.stdout or "")[-2000:]
        report["train_stderr"] = (proc.stderr or "")[-2000:]
        if proc.returncode != 0:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
            return proc.returncode

    bench_out = args.output.with_name("nightly_benchmark.json")
    proc = subprocess.run(
        [
            sys.executable,
            str(scripts / "pubg_regression_benchmark.py"),
            "--output",
            str(bench_out),
        ],
        capture_output=True,
        text=True,
    )
    report["benchmark_rc"] = proc.returncode
    if bench_out.is_file():
        bench = json.loads(bench_out.read_text(encoding="utf-8"))
        summary = bench.get("summary") or {}
        report["benchmark_summary"] = summary
        report["good_accepted_rate"] = summary.get("good_accepted_rate")
        report["bad_accepted_hits"] = summary.get("bad_accepted_hits")
    else:
        report["benchmark_error"] = (proc.stderr or proc.stdout or "")[-1000:]
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return proc.returncode or 1

    from pubg_ranker_champion import (
        compare_benchmark,
        load_champion_meta,
        promote_challenger,
        register_candidate,
    )

    if candidate.is_file():
        archived = register_candidate(candidate, tag="nightly")
        report["candidate"] = str(archived)

    if args.promote and os.environ.get("PUBG_RANKER_AUTO_PROMOTE", "0") == "1":
        champ_meta = load_champion_meta()
        ok, reasons = compare_benchmark(champ_meta, report.get("benchmark_summary") or report)
        report["promote_ok"] = ok
        report["promote_reasons"] = reasons
        if ok or args.force:
            promoted, msg = promote_challenger(
                candidate,
                benchmark_report=report.get("benchmark_summary") or report,
                force=args.force,
            )
            report["promoted"] = promoted
            report["promote_msg"] = msg
        else:
            report["promoted"] = False
    else:
        report["promoted"] = False
        report["promote_skipped"] = "PUBG_RANKER_AUTO_PROMOTE!=1 or --promote not set"

    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("benchmark_rc") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
