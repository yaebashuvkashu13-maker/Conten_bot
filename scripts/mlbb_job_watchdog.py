#!/usr/bin/env python3
"""Safe MLBB job nudge — never kill feed/ingest owned by continuous worker."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time
from pathlib import Path

DATA = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
NUDGE_LOG = Path(os.environ.get("MLBB_NUDGE_LOG", str(DATA / "logs/mlbb_job_nudge.log")))

WORKER_CHILD_PATTERNS = (
    "mlbb_calibration_feed.py",
    "mlbb_youtube_shorts_ingest.py",
    "mlbb_hero_shorts_montage.py",
)


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        NUDGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with NUDGE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def worker_running() -> bool:
    proc = subprocess.run(
        ["pgrep", "-f", "mlbb_continuous_worker.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool((proc.stdout or "").strip())


def vod_only_mode() -> bool:
    return (
        os.environ.get("MLBB_VOD_ONLY", "0") == "1"
        and os.environ.get("MLBB_VOD_DISABLED", "1") != "1"
    )


def kill_shorts_pipeline_orphans() -> int:
    """VOD-only: Shorts worker/feed/ingest must not run."""
    if not vod_only_mode():
        return 0
    killed = 0
    for pattern in (
        "mlbb_continuous_worker.py",
        "mlbb_calibration_feed.py",
        "mlbb_youtube_shorts_ingest.py",
        "mlbb_hero_shorts_montage.py",
    ):
        for pid in pids_matching(pattern):
            kill_pid_tree(pid, label="vod_only_shorts", reason=pattern)
            killed += 1
    return killed


def load_avg_1m() -> float:
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def proc_age_sec(pid: int) -> float:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().split()
        start_ticks = int(stat[21])
        uptime = Path("/proc/uptime").read_text().split()[0]
        hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        return max(0.0, float(uptime) - start_ticks / hz)
    except (OSError, ValueError, IndexError):
        return -1.0


def kill_pid_tree(pid: int, *, label: str, reason: str) -> None:
    _log(f"kill {label} pid={pid} reason={reason}")
    try:
        os.killpg(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return
    time.sleep(2)
    try:
        os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def pids_matching(pattern: str) -> list[int]:
    proc = subprocess.run(
        ["pgrep", "-f", pattern],
        capture_output=True,
        text=True,
        check=False,
    )
    out: list[int] = []
    for line in (proc.stdout or "").split():
        try:
            out.append(int(line.strip()))
        except ValueError:
            continue
    return out


def dedupe_workers() -> int:
    """Keep one continuous worker — duplicates race and kill feed."""
    pids = pids_matching("mlbb_continuous_worker.py")
    if len(pids) <= 1:
        return 0
    pids.sort()
    killed = 0
    for pid in pids[:-1]:
        kill_pid_tree(pid, label="duplicate_worker", reason="extra mlbb_continuous_worker")
        killed += 1
    return killed


def kill_orphans(pattern: str) -> int:
    if worker_running() and pattern in WORKER_CHILD_PATTERNS:
        return 0
    if vod_only_mode() and pattern in (
        "mlbb_vod_segment_feed.py",
        "smart_video_editor.py",
    ):
        return 0
    killed = 0
    for pid in pids_matching(pattern):
        kill_pid_tree(pid, label="orphan", reason=pattern)
        killed += 1
    return killed


def nudge_stale(pattern: str, max_sec: float) -> bool:
    if worker_running() and pattern in WORKER_CHILD_PATTERNS:
        return False
    for pid in pids_matching(pattern):
        age = proc_age_sec(pid)
        if age >= 0 and age < max_sec:
            continue
        kill_pid_tree(pid, label="stale", reason=f"{pattern} age={age:.0f}s")
        return True
    return False


def nudge_high_load() -> int:
    load_max = float(os.environ.get("MLBB_NUDGE_LOAD_MAX", "0"))
    if load_max <= 0 or load_avg_1m() < load_max:
        return 0
    if worker_running():
        _log(f"high_load avg={load_avg_1m():.1f} skip_kill worker_manages_jobs")
        return 0
    _log(f"high_load avg={load_avg_1m():.1f} threshold={load_max}")
    killed = 0
    for pattern in WORKER_CHILD_PATTERNS:
        for pid in pids_matching(pattern):
            age = proc_age_sec(pid)
            min_age = float(os.environ.get("MLBB_NUDGE_LOAD_MIN_AGE_SEC", "600"))
            if age >= min_age:
                kill_pid_tree(pid, label="high_load", reason=pattern)
                killed += 1
    return killed


def dedupe_ingests() -> int:
    """Keep one ingest — zombies were starving the VPS."""
    pids = pids_matching("mlbb_youtube_shorts_ingest.py")
    if len(pids) <= 1:
        return 0
    pids.sort()
    killed = 0
    for pid in pids[:-1]:
        kill_pid_tree(pid, label="duplicate_ingest", reason="extra mlbb_youtube_shorts_ingest")
        killed += 1
    return killed


def nudge_all() -> list[str]:
    actions: list[str] = []
    if vod_only_mode():
        shorts_killed = kill_shorts_pipeline_orphans()
        if shorts_killed:
            actions.append(f"vod_only_kill_shorts={shorts_killed}")
    dup = dedupe_workers()
    if dup:
        actions.append(f"duplicate_worker={dup}")
    ing = dedupe_ingests()
    if ing:
        actions.append(f"duplicate_ingest={ing}")
    stale_feed = float(os.environ.get("MLBB_FEED_STALE_SEC", "900"))
    stale_ingest = float(os.environ.get("MLBB_INGEST_STALE_SEC", "2400"))
    if nudge_stale("mlbb_calibration_feed.py", stale_feed):
        actions.append("stale_feed")
    if nudge_stale("mlbb_youtube_shorts_ingest.py", stale_ingest):
        actions.append("stale_ingest")
    for pattern in ("mlbb_vod_segment_feed.py", "smart_video_editor.py"):
        orphans = kill_orphans(pattern)
        if orphans:
            actions.append(f"orphan_{pattern}={orphans}")
    hl = nudge_high_load()
    if hl:
        actions.append(f"high_load={hl}")
    if actions:
        _log("nudge_done " + " ".join(actions))
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description="MLBB safe job watchdog")
    parser.add_argument("--nudge", action="store_true")
    args = parser.parse_args()
    if args.nudge:
        acts = nudge_all()
        print("actions", acts if acts else "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
