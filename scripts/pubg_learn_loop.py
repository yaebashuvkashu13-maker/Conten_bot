#!/usr/bin/env python3
"""One-shot learnable loop: export dataset → train → regression bench → report.

This is the product path for owner window ratings (not hand-tuned gate spam).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def main() -> int:
    parser = argparse.ArgumentParser(description="PUBG learnable ranker loop")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="JSONL dataset path (default PUBG_RANKER_DATASET)",
    )
    parser.add_argument("--extract-features", action="store_true")
    parser.add_argument("--skip-benchmark", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/root/data/pubg/learn_loop_report.json"),
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=None,
        help="Where to write the trained joblib (default PUBG_RANKER_MODEL)",
    )
    args = parser.parse_args()

    report: dict = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "loop": "export→train→benchmark",
    }

    export_cmd = [sys.executable, str(SCRIPTS / "pubg_ranker_dataset.py"), "export"]
    if args.dataset is not None:
        export_cmd.extend(["--output", str(args.dataset)])
    if args.extract_features:
        export_cmd.append("--extract-features")
    rc, out, err = _run(export_cmd)
    report["export_rc"] = rc
    try:
        report["export"] = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    except json.JSONDecodeError:
        report["export_stdout"] = out[-1500:]
        report["export_stderr"] = err[-1500:]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return rc or 1
    if rc != 0:
        report["export_stderr"] = err[-1500:]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return rc

    dataset_path = Path(
        (report.get("export") or {}).get("path")
        or args.dataset
        or os.environ.get("PUBG_RANKER_DATASET", "/root/data/pubg/ranker_dataset/moments_v1.jsonl")
    )
    train_cmd = [
        sys.executable,
        str(SCRIPTS / "pubg_moment_ranker.py"),
        "--train",
        "--dataset",
        str(dataset_path),
    ]
    if args.model is not None:
        os.environ["PUBG_RANKER_MODEL"] = str(args.model)
    rc, out, err = _run(train_cmd)
    report["train_rc"] = rc
    try:
        report["train"] = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    except json.JSONDecodeError:
        report["train_stdout"] = out[-2000:]
        report["train_stderr"] = err[-2000:]
    if rc != 0:
        report["train_stderr"] = (report.get("train_stderr") or err)[-2000:]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return rc

    if not args.skip_benchmark and (SCRIPTS / "pubg_regression_benchmark.py").is_file():
        bench_out = args.output.with_name("learn_loop_benchmark.json")
        rc, out, err = _run(
            [
                sys.executable,
                str(SCRIPTS / "pubg_regression_benchmark.py"),
                "--output",
                str(bench_out),
            ]
        )
        report["benchmark_rc"] = rc
        if bench_out.is_file():
            try:
                bench = json.loads(bench_out.read_text(encoding="utf-8"))
                report["benchmark_summary"] = bench.get("summary") or bench
            except json.JSONDecodeError:
                report["benchmark_error"] = "invalid_json"
        else:
            report["benchmark_stderr"] = err[-1500:]
    else:
        report["benchmark_skipped"] = True

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    status = report.get("train", {}).get("status")
    return 0 if status in {"trained", "unchanged"} and report.get("train_rc", 1) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
