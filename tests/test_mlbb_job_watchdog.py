"""Tests for mlbb_job_watchdog stale nudge behavior."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mlbb_job_watchdog as wd  # noqa: E402


def test_nudge_stale_kills_even_when_worker_running() -> None:
    with (
        patch.object(wd, "worker_running", return_value=True),
        patch.object(wd, "pids_matching", return_value=[999]),
        patch.object(wd, "proc_age_sec", return_value=5000.0),
        patch.object(wd, "kill_pid_tree") as kill,
    ):
        assert wd.nudge_stale("mlbb_calibration_feed.py", 900) is True
    kill.assert_called_once()


def test_nudge_stale_respects_max_age() -> None:
    with (
        patch.object(wd, "pids_matching", return_value=[999]),
        patch.object(wd, "proc_age_sec", return_value=100.0),
        patch.object(wd, "kill_pid_tree") as kill,
    ):
        assert wd.nudge_stale("mlbb_youtube_shorts_ingest.py", 2400) is False
    kill.assert_not_called()
