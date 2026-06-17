#!/usr/bin/env python3
"""Auto-nudge hung MLBB jobs — kill stale ingest/feed/vod, orphans, learn_apply zombies."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time
from pathlib import Path

DATA = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
NUDGE_LOG = Path(os.environ.get("MLBB_NUDGE_LOG", str(DATA / "logs/mlbb_job_nudge.log")))

JOBS: dict[str, tuple[Path, str, str, str]] = {
    "ingest": (
        Path(os.environ.get("MLBB_SHORTS_INGEST_LOCK", str(DATA / "youtube_shorts_ingest.lock"))),
        "mlbb_youtube_shorts_ingest.py",
        "MLBB_INGEST_STALE_SEC",
        "900",
    ),
    "feed": (
        Path(os.environ.get("MLBB_CALIBRATION_FEED_LOCK", str(DATA / "calibration_feed.lock"))),
        "mlbb_calibration_feed.py",
        "MLBB_FEED_STALE_SEC",
        "300",
    ),
    "vod": (
        Path(os.environ.get("MLBB_VOD_FEED_LOCK", str(DATA / "vod_segment_feed.lock"))),
        "mlbb_vod_segment_feed.py",
        "MLBB_VOD_STALE_SEC",
        "1200",
    ),
}


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        NUDGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with NUDGE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def proc_age_sec(pid: int) -> float:
    try:
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            start_ticks = int(fh.read().split()[21])
        clk = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        return max(0.0, uptime - (start_ticks / clk))
    except (OSError, ValueError, IndexError):
        return -1.0


def load_avg_1m() -> float:
    try:
        return float(os.getloadavg()[0])
    except OSError:
        return 0.0


def kill_pid_tree(pid: int, *, label: str, reason: str) -> None:
    _log(f"nudge {label} pid={pid} reason={reason}")
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(1)
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        for line in (out.stdout or "").split():
            try:
                os.kill(int(line.strip()), signal.SIGKILL)
            except (ValueError, OSError):
                pass
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def lock_holder(lock_path: Path) -> int | None:
    if not lock_path.exists():
        return None
    try:
        pid = int(lock_path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return pid
    except (ProcessLookupError, ValueError, OSError):
        lock_path.unlink(missing_ok=True)
        return None


def nudge_lock(name: str, lock_path: Path, pattern: str, max_sec: float) -> bool:
    pid = lock_holder(lock_path)
    if pid is None:
        return False
    age = proc_age_sec(pid)
    if age < 0:
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            age = 0.0
    if age < max_sec:
        return False
    kill_pid_tree(pid, label=name, reason=f"stale age={age:.0f}s max={max_sec:.0f}s")
    lock_path.unlink(missing_ok=True)
    return True


def kill_orphans(pattern: str, lock_path: Path) -> int:
    holder = lock_holder(lock_path)
    killed = 0
    try:
        out = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        for line in (out.stdout or "").split():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if holder is not None and pid == holder:
                continue
            kill_pid_tree(pid, label="orphan", reason=pattern)
            killed += 1
    except Exception:
        pass
    return killed


def kill_learn_apply_zombies(max_sec: float) -> int:
    killed = 0
    try:
        out = subprocess.run(
            ["pgrep", "-f", "mlbb_learn_apply.sh"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        for line in (out.stdout or "").split():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            age = proc_age_sec(pid)
            if age < 0 or age >= max_sec:
                kill_pid_tree(pid, label="learn_apply", reason=f"zombie age={age:.0f}s")
                killed += 1
    except Exception:
        pass
    return killed


def nudge_high_load() -> int:
    load_max = float(os.environ.get("MLBB_NUDGE_LOAD_MAX", "6"))
    if load_max <= 0 or load_avg_1m() < load_max:
        return 0
    _log(f"high_load avg={load_avg_1m():.1f} threshold={load_max}")
    killed = 0
    for name, (lock_path, pattern, _env_key, _default) in JOBS.items():
        pid = lock_holder(lock_path)
        if pid is None:
            continue
        age = proc_age_sec(pid)
        min_age = float(os.environ.get("MLBB_NUDGE_LOAD_MIN_AGE_SEC", "180"))
        if age >= min_age:
            kill_pid_tree(pid, label=name, reason=f"high_load age={age:.0f}s")
            lock_path.unlink(missing_ok=True)
            killed += 1
    return killed


def nudge_all() -> list[str]:
    actions: list[str] = []
    for name, (lock_path, pattern, env_key, default) in JOBS.items():
        max_sec = float(os.environ.get(env_key, default))
        if nudge_lock(name, lock_path, pattern, max_sec):
            actions.append(f"stale_{name}")
        orphans = kill_orphans(pattern, lock_path)
        if orphans:
            actions.append(f"orphan_{name}={orphans}")
    zombies = kill_learn_apply_zombies(float(os.environ.get("MLBB_LEARN_APPLY_MAX_SEC", "600")))
    if zombies:
        actions.append(f"learn_apply={zombies}")
    hl = nudge_high_load()
    if hl:
        actions.append(f"high_load={hl}")
    if actions:
        _log("nudge_done " + " ".join(actions))
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description="MLBB job watchdog nudge")
    parser.add_argument("--nudge", action="store_true", help="Kill stale/orphan jobs")
    args = parser.parse_args()
    if args.nudge:
        acts = nudge_all()
        print("actions", acts if acts else "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
