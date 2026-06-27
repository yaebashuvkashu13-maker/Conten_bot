#!/usr/bin/env python3
"""Retry, backoff and JSON job-state helpers for VPS montage pipelines."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable


def getenv_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def getenv_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def retry_sleep_sec(attempt: int, *, base: float = 30.0, cap: float = 600.0) -> float:
    return min(cap, base * (1.5 ** max(0, attempt - 1)))


def load_json_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def save_json_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def job_is_ok(state: dict[str, Any], job_key: str) -> bool:
    jobs = state.get("jobs") or {}
    entry = jobs.get(job_key) or {}
    return entry.get("status") == "ok"


def mark_job(
    state: dict[str, Any],
    job_key: str,
    *,
    status: str,
    path: Path,
    output: str = "",
    error: str = "",
    attempts: int = 0,
) -> None:
    jobs = dict(state.get("jobs") or {})
    jobs[job_key] = {
        "status": status,
        "output": output,
        "error": error[-500:] if error else "",
        "attempts": attempts,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    state["jobs"] = jobs
    save_json_state(path, state)


def count_ok_jobs(state: dict[str, Any]) -> int:
    jobs = state.get("jobs") or {}
    return sum(1 for entry in jobs.values() if entry.get("status") == "ok")


def pipeline_complete(state: dict[str, Any], total_jobs: int) -> bool:
    if state.get("completed"):
        return True
    return count_ok_jobs(state) >= total_jobs


def run_until_success(
    label: str,
    fn: Callable[[int], int],
    *,
    max_attempts: int | None = None,
    log: Callable[[str], None] | None = None,
) -> int:
    attempts = max_attempts or getenv_int("PIPELINE_JOB_RETRIES", 5)
    last_code = 1
    for attempt in range(1, attempts + 1):
        code = fn(attempt)
        if code == 0:
            return 0
        last_code = code
        if attempt < attempts:
            delay = retry_sleep_sec(attempt)
            msg = f"{label} attempt {attempt}/{attempts} failed rc={code}, retry in {delay:.0f}s"
            if log:
                log(msg)
            else:
                print(msg, flush=True)
            time.sleep(delay)
    return last_code
