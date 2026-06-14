#!/usr/bin/env python3
"""Daily owner status: sends, precision, worker health."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_telegram_send import load_env, send_message
from mlbb_vod_segment_store import stats as vseg_stats


def worker_running() -> bool:
    proc = subprocess.run(
        ["pgrep", "-f", "mlbb_continuous_worker.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool((proc.stdout or "").strip())


def worker_state() -> dict:
    path = Path("/root/data/mlbb/mlbb_continuous_state.json")
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def state_age_min() -> float | None:
    state = worker_state()
    ts = state.get("updated_at", "")
    if not ts:
        return None
    try:
        updated = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - updated).total_seconds() / 60.0
    except ValueError:
        return None


def learning_stats() -> dict:
    try:
        from mlbb_learning_first import daily_send_count, enabled, max_daily_sends, precision_7d, sends_allowed

        return {
            "sent_today": daily_send_count(),
            "cap": max_daily_sends(),
            "precision_7d": precision_7d(),
            "learning_first": enabled(),
            "sends_allowed": sends_allowed(),
        }
    except ImportError:
        return {"sent_today": 0, "cap": 500, "precision_7d": 0.0, "learning_first": False, "sends_allowed": True}


def build_report(env: dict | None = None) -> str:
    import os

    env = env or load_env()
    vs = vseg_stats()
    ls = learning_stats()
    state = worker_state()
    age = state_age_min()

    worker_ok = worker_running()
    worker_line = "✅ жив" if worker_ok else "❌ мёртв"
    if worker_ok and age is not None and age > 12:
        worker_line = f"⚠️ heartbeat {age:.0f}m ago"

    calib = {}
    try:
        from mlbb_calibration_store import stats as calib_stats

        calib = calib_stats()
    except Exception:
        pass

    return (
        "📋 MLBB Bot — дневной статус\n"
        f"Worker: {worker_line}\n"
        f"Отправки сегодня: {ls['sent_today']}/{ls['cap']}\n"
        f"precision_7d: {ls['precision_7d']:.0%}\n"
        f"Метки VOD: 👍{vs['feedback_yes']} 👎{vs['feedback_no']}\n"
        f"Метки Shorts: 👍{calib.get('feedback_yes', 0)} 👎{calib.get('feedback_no', 0)} "
        f"pending={calib.get('pending', '?')}\n"
        f"Kill-first: {os.environ.get('MLBB_VOD_KILL_FIRST', '1')} | "
        f"batch={os.environ.get('MLBB_VOD_BATCH_MAX', '40')}\n"
        f"pending Shorts queue: {state.get('pending_shorts', '?')}\n"
        f"pipelines: ingest={'on' if state.get('ingest_running') else 'off'} "
        f"vod={'on' if state.get('vod_running') else 'off'} "
        f"feed={'on' if state.get('feed_running') else 'off'}"
    )


def main() -> int:
    load_env()
    text = build_report()
    print(text)
    if "--telegram" in sys.argv:
        if send_message(text):
            print("telegram_sent=1")
        else:
            print("telegram_sent=0", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
