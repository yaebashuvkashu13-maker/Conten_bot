from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mlbb_job_watchdog import JOBS, load_avg_1m  # noqa: E402


def test_jobs_configured() -> None:
    assert "ingest" in JOBS
    assert "feed" in JOBS
    assert JOBS["ingest"][2] == "MLBB_INGEST_STALE_SEC"


def test_load_avg_non_negative() -> None:
    assert load_avg_1m() >= 0.0
