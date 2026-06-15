#!/usr/bin/env python3
"""Autonomic recovery — no manual revive. Run from cron/watchdog every 2-3 min."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BIN = Path("/usr/local/bin")
PY = sys.executable
DATA = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
LOG = Path(os.environ.get("MLBB_HEALTH_GUARD_LOG", str(DATA / "logs/mlbb_health_guard.log")))


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _script(name: str) -> Path:
    p = BIN / name
    if p.exists():
        return p
    return Path(__file__).resolve().parent / name


def _load_env() -> dict[str, str]:
    env = dict(os.environ)
    env_file = Path("/root/.video_bot.env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    env.setdefault("PYTHONPATH", str(BIN))
    env.setdefault("MLBB_DATA_ROOT", str(DATA))
    return env


def _pending_count() -> int:
    from mlbb_calibration_store import pending_candidates

    return len(pending_candidates(limit=500, repair=False))


def _index_disk(env: dict[str, str]) -> int:
    from mlbb_calibration_store import index_unlabeled_disk_shorts, rebuild_index_from_disk

    rebuild_index_from_disk()
    return index_unlabeled_disk_shorts(limit=int(env.get("MLBB_DISK_INDEX_LIMIT", "24")))


def _run_ingest(env: dict[str, str], *, starvation: bool = False) -> bool:
    script = _script("mlbb_youtube_shorts_ingest.py")
    cmd = [
        PY,
        str(script),
        "--incremental",
        "--max-downloads",
        env.get("MLBB_STEADY_INGEST_MAX_DOWNLOADS", "4"),
        "--max-per-query",
        env.get("MLBB_STEADY_INGEST_MAX_PER_QUERY", "20"),
        "--download-delay",
        env.get("MLBB_STEADY_INGEST_DOWNLOAD_DELAY", "4"),
        "--search-delay",
        env.get("MLBB_STEADY_INGEST_SEARCH_DELAY", "5"),
    ]
    run_env = {
        **env,
        "MLBB_SHORTS_CALIBRATION_BURST": "0",
        "MLBB_INGEST_SKIP_IF_PENDING": "0",
        "MLBB_STEADY_MODE": "1",
    }
    if starvation:
        run_env["MLBB_STARVATION_INGEST"] = "1"
    try:
        subprocess.run(cmd, env=run_env, timeout=int(env.get("MLBB_HEALTH_INGEST_TIMEOUT", "840")), check=False)
        return True
    except subprocess.TimeoutExpired:
        _log("ingest recovery timeout")
        return False


def _run_feed(env: dict[str, str]) -> bool:
    script = _script("mlbb_calibration_feed.py")
    run_env = {**env, "MLBB_FEED_REBUILD": "1"}
    try:
        subprocess.run(
            [PY, str(script)],
            env=run_env,
            timeout=int(env.get("MLBB_HEALTH_FEED_TIMEOUT", "480")),
            check=False,
        )
        return True
    except subprocess.TimeoutExpired:
        _log("feed recovery timeout")
        return False


def _nudge_jobs() -> list[str]:
    try:
        from mlbb_job_watchdog import nudge_all

        return nudge_all()
    except Exception as exc:
        _log(f"nudge failed: {exc}")
        return []


def _notify(env: dict[str, str], text: str) -> None:
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        return
    if env.get("MLBB_HEALTH_NOTIFY", "1") != "1":
        return
    subprocess.run(
        [
            "curl",
            "-sS",
            "--noproxy",
            "*",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"text={text[:3900]}",
            f"https://api.telegram.org/bot{token}/sendMessage",
        ],
        env={k: v for k, v in env.items() if "proxy" not in k.lower()},
        check=False,
        timeout=30,
    )


def recover(*, reason: str, env: dict[str, str] | None = None) -> list[str]:
    from mlbb_pipeline_health import record_recovery

    env = env or _load_env()
    actions: list[str] = []

    nudged = _nudge_jobs()
    if nudged:
        actions.append("nudge:" + ",".join(nudged))

    for lock in (
        DATA / "youtube_shorts_ingest.lock",
        DATA / "calibration_feed.lock",
        DATA / "vod_segment_feed.lock",
    ):
        if lock.exists():
            try:
                pid = int(lock.read_text(encoding="utf-8").strip())
                os.kill(pid, 0)
            except (ProcessLookupError, ValueError, OSError):
                lock.unlink(missing_ok=True)
                actions.append(f"cleared_lock:{lock.name}")
            else:
                try:
                    age = time.time() - lock.stat().st_mtime
                except OSError:
                    age = 0.0
                stale = float(env.get("MLBB_INGEST_STALE_SEC" if "ingest" in lock.name else "MLBB_FEED_STALE_SEC", "600"))
                if age > stale:
                    try:
                        os.kill(pid, 15)
                    except OSError:
                        pass
                    lock.unlink(missing_ok=True)
                    actions.append(f"killed_stale:{lock.name}")

    added = _index_disk(env)
    actions.append(f"disk_index={added}")
    pending = _pending_count()
    actions.append(f"pending={pending}")

    if pending < int(env.get("MLBB_STEADY_MIN_PENDING", "4")):
        if _run_ingest(env, starvation=True):
            actions.append("ingest_starvation")
        pending = _pending_count()
        actions.append(f"pending_after_ingest={pending}")

    batch = int(env.get("MLBB_CALIBRATION_BATCH", "4"))
    min_feed = int(env.get("MLBB_STEADY_MIN_SEND_PENDING", "1"))
    if pending >= min(min_feed, batch):
        if _run_feed(env):
            actions.append("feed_forced")

    record_recovery(reason=reason, actions=actions)
    _log(f"recovery reason={reason} actions={' | '.join(actions)}")
    _notify(env, f"🔧 MLBB auto-recovery: {reason}\n" + "\n".join(actions[:6]))
    return actions


def _disk_status(env: dict[str, str]) -> dict:
    """Check disk/inode headroom for overnight stability."""
    import shutil

    paths = [
        Path(env.get("MLBB_DATA_ROOT", str(DATA))),
        Path(env.get("MLBB_SHORTS_ROOT", "/root/datasets/mlbb/youtube_shorts")),
    ]
    worst_pct = 0.0
    details: list[str] = []
    for p in paths:
        try:
            usage = shutil.disk_usage(p if p.exists() else p.parent)
            pct = usage.used / usage.total * 100.0 if usage.total else 0.0
            worst_pct = max(worst_pct, pct)
            details.append(f"{p.name}:{pct:.0f}%")
        except OSError:
            continue
    warn_pct = float(env.get("MLBB_DISK_WARN_PCT", "88"))
    crit_pct = float(env.get("MLBB_DISK_CRIT_PCT", "95"))
    level = "ok"
    if worst_pct >= crit_pct:
        level = "critical"
    elif worst_pct >= warn_pct:
        level = "warn"
    return {"level": level, "used_pct": round(worst_pct, 1), "details": details}


def check(env: dict[str, str] | None = None) -> dict:
    from mlbb_pipeline_health import needs_recovery, silence_sec, snapshot

    env = env or _load_env()
    pending = _pending_count()
    health = snapshot()
    need, why = needs_recovery(pending=pending)
    disk = _disk_status(env)
    if disk["level"] == "critical" and not need:
        need, why = True, f"disk_critical:{disk['used_pct']}%"
    out = {
        "pending": pending,
        "silence_sec": int(silence_sec()),
        "need_recovery": need,
        "reason": why,
        "health": health,
        "disk": disk,
    }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="MLBB health guard")
    parser.add_argument("--check", action="store_true", help="Print health JSON")
    parser.add_argument("--recover", action="store_true", help="Recover if needed")
    parser.add_argument("--force-recover", action="store_true", help="Always run recovery")
    args = parser.parse_args()
    env = _load_env()

    if args.check or (not args.recover and not args.force_recover):
        print(json.dumps(check(env), ensure_ascii=False, indent=2))
        if not args.recover and not args.force_recover:
            return 0

    status = check(env)
    if args.force_recover or status["need_recovery"]:
        recover(reason=status["reason"] if status["need_recovery"] else "forced", env=env)
        return 0
    _log(f"ok pending={status['pending']} silence={status['silence_sec']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
