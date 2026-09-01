#!/usr/bin/env python3
"""Nightly champion/challenger training, regression gate and atomic promotion."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def champion_path() -> Path:
    return Path(os.environ.get("PUBG_RANKER_MODEL", "/root/data/pubg/pubg_moment_ranker.joblib"))


def candidate_path() -> Path:
    return Path(
        os.environ.get(
            "PUBG_RANKER_STAGING",
            str(champion_path().with_name(champion_path().stem + ".candidate.joblib")),
        )
    )


def baseline_path() -> Path:
    return Path(
        os.environ.get(
            "PUBG_RANKER_BASELINE",
            "/root/data/pubg/regression_baseline.json",
        )
    )


def _load_joblib_meta(path: Path) -> dict[str, Any]:
    try:
        import joblib

        value = joblib.load(path)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _read_summary(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        summary = data.get("summary") or {}
        return {str(key): float(value) for key, value in summary.items() if isinstance(value, (int, float))}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def should_promote(
    candidate: dict[str, Any],
    champion: dict[str, Any],
    regression: dict[str, float],
    baseline: dict[str, float],
) -> tuple[bool, str]:
    minimum = float(os.environ.get("PUBG_RANKER_MIN_OOF_BALANCED_ACCURACY", "0.52"))
    candidate_oof = float(candidate.get("oof_balanced_accuracy") or 0.0)
    champion_oof = float(champion.get("oof_balanced_accuracy") or 0.0)
    if candidate_oof < minimum:
        return False, f"candidate_oof={candidate_oof:.3f}<min{minimum:.3f}"
    allowed_oof_drop = float(os.environ.get("PUBG_RANKER_MAX_OOF_DROP", "0.01"))
    if champion and candidate_oof + allowed_oof_drop < champion_oof:
        return False, f"candidate_oof={candidate_oof:.3f}<champion{champion_oof:.3f}"
    if not baseline:
        allow_first = os.environ.get("PUBG_RANKER_ALLOW_FIRST_PROMOTE", "0") == "1"
        return (allow_first, "first_promote_allowed" if allow_first else "baseline_missing")
    recall_drop = float(os.environ.get("PUBG_RANKER_MAX_RECALL_DROP", "0.05"))
    bad_growth = float(os.environ.get("PUBG_RANKER_MAX_BAD_GROWTH", "0.02"))
    current_recall = float(regression.get("accepted_recall", 0.0))
    baseline_recall = float(baseline.get("accepted_recall", 0.0))
    if current_recall + recall_drop < baseline_recall:
        return False, f"accepted_recall={current_recall:.3f}<baseline{baseline_recall:.3f}"
    current_bad = float(regression.get("bad_accept_rate", 1.0))
    baseline_bad = float(baseline.get("bad_accept_rate", 1.0))
    if current_bad > baseline_bad + bad_growth:
        return False, f"bad_accept={current_bad:.3f}>baseline{baseline_bad:.3f}"
    return True, "promotion_gates_pass"


def _atomic_promote(candidate: Path, champion: Path) -> Path | None:
    champion.parent.mkdir(parents=True, exist_ok=True)
    previous = champion.with_name(champion.stem + ".prev.joblib")
    if champion.exists():
        shutil.copy2(champion, previous)
    with tempfile.NamedTemporaryFile(dir=champion.parent, delete=False) as handle:
        temp = Path(handle.name)
    try:
        shutil.copy2(candidate, temp)
        os.replace(temp, champion)
    finally:
        temp.unlink(missing_ok=True)
    candidate_json = candidate.with_suffix(".json")
    if candidate_json.exists():
        shutil.copy2(candidate_json, champion.with_suffix(".json"))
    return previous if previous.exists() else None


def run(*, force_train: bool = False) -> dict[str, Any]:
    if os.environ.get("PUBG_RANKER_AUTO_PROMOTE", "1") != "1":
        return {"status": "disabled"}
    repo = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
    candidate = candidate_path()
    champion = champion_path()
    env = dict(os.environ)
    env["PUBG_RANKER_MODEL"] = str(candidate)
    train_cmd = [
        sys.executable,
        str(repo / "scripts" / "pubg_moment_ranker.py"),
        "--train" if force_train else "--train-if-changed",
    ]
    trained = subprocess.run(train_cmd, env=env, text=True, capture_output=True, timeout=4 * 3600)
    if trained.returncode != 0:
        return {"status": "train_failed", "detail": (trained.stdout + trained.stderr)[-2000:]}

    candidate_meta = _load_joblib_meta(candidate)
    champion_meta = _load_joblib_meta(champion)
    regression_path = candidate.with_name("regression_candidate.json")
    regression_cmd = [
        sys.executable,
        str(repo / "scripts" / "pubg_regression_benchmark.py"),
        "--labels",
        str(repo / "data" / "pubg_regression_labels.json"),
        "--online-labels",
        os.environ.get("PUBG_OWNER_LABELS_PATH", "/root/data/pubg/pubg_owner_labels.json"),
        "--vod-root",
        os.environ.get("PUBG_REGRESSION_VOD_ROOT", "/root/data/pubg/regression_vods"),
        "--output",
        str(regression_path),
    ]
    regression_env = dict(os.environ)
    regression_env["PUBG_RANKER_MODEL"] = str(candidate)
    checked = subprocess.run(
        regression_cmd,
        env=regression_env,
        text=True,
        capture_output=True,
        timeout=int(os.environ.get("PUBG_RANKER_REGRESSION_TIMEOUT_SEC", "14400")),
    )
    if checked.returncode != 0:
        return {
            "status": "regression_failed",
            "detail": (checked.stdout + checked.stderr)[-2000:],
        }

    regression = _read_summary(regression_path)
    baseline = _read_summary(baseline_path())
    ok, reason = should_promote(candidate_meta, champion_meta, regression, baseline)
    report: dict[str, Any] = {
        "status": "promoted" if ok else "rejected",
        "reason": reason,
        "candidate_oof": candidate_meta.get("oof_balanced_accuracy"),
        "champion_oof": champion_meta.get("oof_balanced_accuracy"),
        "regression": regression,
        "baseline": baseline,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if ok:
        previous = _atomic_promote(candidate, champion)
        report["previous"] = str(previous) if previous else None
    report_path = champion.with_name("ranker_promotion_last.json")
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-train", action="store_true")
    args = parser.parse_args()
    report = run(force_train=args.force_train)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] in {"promoted", "rejected", "disabled"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
