from __future__ import annotations

from pathlib import Path

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pipeline_retry import (
    count_ok_jobs,
    job_is_ok,
    load_json_state,
    mark_job,
    pipeline_complete,
    retry_sleep_sec,
    save_json_state,
)


def test_retry_sleep_grows(tmp_path: Path) -> None:
    assert retry_sleep_sec(1) < retry_sleep_sec(3)
    assert retry_sleep_sec(99) <= 600.0


def test_job_state_roundtrip(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state: dict = {"jobs": {}, "total_jobs": 2}
    mark_job(state, "genshin:v1", status="ok", path=state_path, output="out.mp4", attempts=1)
    loaded = load_json_state(state_path)
    assert job_is_ok(loaded, "genshin:v1")
    assert count_ok_jobs(loaded) == 1
    assert not pipeline_complete(loaded, 2)
    mark_job(state, "pubg:v1", status="ok", path=state_path, output="p.mp4", attempts=1)
    loaded = load_json_state(state_path)
    loaded["completed"] = True
    save_json_state(state_path, loaded)
    assert pipeline_complete(load_json_state(state_path), 2)
