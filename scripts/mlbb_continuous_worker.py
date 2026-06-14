#!/usr/bin/env python3
"""MLBB 24/7 worker — ingest, VOD segment scan, calibration feed in parallel."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LOG = Path(os.environ.get("MLBB_CONTINUOUS_LOG", "/root/data/mlbb/mlbb_continuous_worker.log"))
ENV_FILE = Path("/root/.video_bot.env")
BIN = Path("/usr/local/bin")
PY = sys.executable
PAUSED_PIPELINES = Path("/root/data/mlbb/PAUSED_PIPELINES")

PIDFILE = Path(os.environ.get("MLBB_CONTINUOUS_PID", "/root/data/mlbb/mlbb_continuous_worker.pid"))
WORKER_LOCK = Path(os.environ.get("MLBB_CONTINUOUS_LOCK", "/root/data/mlbb/mlbb_continuous_worker.lock"))
VOD_LOCK = Path(os.environ.get("MLBB_VOD_FEED_LOCK", "/root/data/mlbb/vod_segment_feed.lock"))
INGEST_LOCK = Path(os.environ.get("MLBB_SHORTS_INGEST_LOCK", "/root/data/mlbb/youtube_shorts_ingest.lock"))
FEED_LOCK = Path(os.environ.get("MLBB_CALIBRATION_FEED_LOCK", "/root/data/mlbb/calibration_feed.lock"))
LOOP_SEC = float(os.environ.get("MLBB_CONTINUOUS_LOOP_SEC", "4"))
_LAST_INDEX_REBUILD = 0.0


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line)
    print(line, end="")


def load_env_file() -> dict[str, str]:
    env = dict(os.environ)
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        env.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    return env


def base_env() -> dict[str, str]:
    env = load_env_file()
    send_enabled = env.get("MLBB_SEND_ENABLED", "1")
    env.update(
        {
            "CONTENT_BOT_REPO": env.get("CONTENT_BOT_REPO", "/root/content_bot_ml"),
            "MLBB_DATA_ROOT": env.get("MLBB_DATA_ROOT", "/root/data/mlbb"),
            "PYTHONPATH": f"{BIN}:{env.get('CONTENT_BOT_REPO', '/root/content_bot_ml')}/scripts",
            "MLBB_ONLY_MODE": "1",
            "MLBB_LEARNING_FIRST": env.get("MLBB_LEARNING_FIRST", "0"),
            "MLBB_SEND_ENABLED": send_enabled,
            "MLBB_VOD_VARIABLE_LENGTH": "1",
            "MLBB_VOD_LEAD_SEC": "4",
            "MLBB_MAX_DAILY_SENDS": env.get("MLBB_MAX_DAILY_SENDS", "500"),
            "MLBB_VOD_BATCH_MAX": env.get("MLBB_VOD_BATCH_MAX", "40"),
            "MLBB_CALIBRATION_LENIENT": env.get("MLBB_CALIBRATION_LENIENT", "1"),
            "MLBB_VOD_CALIBRATION_LENIENT": env.get("MLBB_VOD_CALIBRATION_LENIENT", "1"),
            "MLBB_KILL_SCAN_SKIP_OCR": env.get("MLBB_KILL_SCAN_SKIP_OCR", "1"),
            "MLBB_CALIBRATION_MIN_SEND_SCORE": env.get("MLBB_CALIBRATION_MIN_SEND_SCORE", "0"),
            "MLBB_CALIBRATION_BATCH": env.get("MLBB_CALIBRATION_BATCH", "12"),
            "MLBB_SHORTS_CALIBRATION_BURST": env.get("MLBB_SHORTS_CALIBRATION_BURST", "1"),
            "HIGHLIGHT_HEATMAP": "0",
            "HIGHLIGHT_USE_OWNER_ANCHORS": "0",
        }
    )
    for key in list(env):
        if "proxy" in key.lower():
            env.pop(key, None)
    return env


class Proc:
    def __init__(self, name: str, cmd: list[str], env: dict[str, str]):
        self.name = name
        self.cmd = cmd
        self.env = env
        self.proc: subprocess.Popen | None = None
        self.last_start = 0.0

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> bool:
        if self.running():
            return False
        LOG.parent.mkdir(parents=True, exist_ok=True)
        out = open(LOG, "a", encoding="utf-8")
        self.proc = subprocess.Popen(
            self.cmd,
            env=self.env,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.last_start = time.time()
        log(f"START {self.name} pid={self.proc.pid}")
        return True

    def reap(self) -> int | None:
        if not self.proc:
            return None
        rc = self.proc.poll()
        if rc is None:
            return None
        log(f"DONE {self.name} rc={rc}")
        self.proc = None
        return rc

    def cooldown_ok(self, sec: float) -> bool:
        if self.running():
            return False
        if self.last_start <= 0:
            return True
        return (time.time() - self.last_start) >= sec


def pending_shorts() -> int:
    global _LAST_INDEX_REBUILD
    from mlbb_calibration_store import pending_candidates, rebuild_index_from_disk

    interval = float(os.environ.get("MLBB_INDEX_REBUILD_SEC", "120"))
    now = time.time()
    if now - _LAST_INDEX_REBUILD >= interval:
        try:
            rebuild_index_from_disk()
        except Exception as exc:
            log(f"rebuild_index_from_disk skipped: {exc}")
        _LAST_INDEX_REBUILD = now
    try:
        return len(pending_candidates(limit=9999))
    except Exception as exc:
        log(f"pending_candidates error: {exc}")
        return 0


def _lock_pid_alive(lock_path: Path) -> bool:
    if not lock_path.exists():
        return False
    try:
        pid = int(lock_path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def vod_feed_running_externally() -> bool:
    return _lock_pid_alive(VOD_LOCK)


def ingest_running_externally() -> bool:
    return _lock_pid_alive(INGEST_LOCK)


def feed_running_externally() -> bool:
    return _lock_pid_alive(FEED_LOCK)


def heavy_job_running() -> bool:
    return (
        ingest_running_externally()
        or vod_feed_running_externally()
    )


def should_start_ingest(*, pending: int, target_pending: int) -> bool:
    """On 4-core hosts, avoid ingest OCR while VOD scan runs unless queue is empty."""
    if os.environ.get("MLBB_ONE_HEAVY_JOB", "1") != "1":
        return True
    if not vod_feed_running_externally():
        return True
    return pending < int(os.environ.get("MLBB_INGEST_FORCE_PENDING", "3"))


def should_start_vod(*, pending: int) -> bool:
    if os.environ.get("MLBB_ONE_HEAVY_JOB", "1") != "1":
        return True
    if not ingest_running_externally():
        return True
    return pending >= int(os.environ.get("MLBB_VOD_PAUSE_WHEN_SHORTS_PENDING", "8"))


def kill_stale_ingest() -> None:
    """Free ingest lock if a previous run hung (blocks worker for hours)."""
    if not INGEST_LOCK.exists():
        return
    try:
        pid = int(INGEST_LOCK.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError, OSError):
        INGEST_LOCK.unlink(missing_ok=True)
        return
    max_sec = float(os.environ.get("MLBB_INGEST_MAX_RUN_SEC", "2400")) + 300
    age = max_sec + 1.0
    try:
        stat = os.stat(f"/proc/{pid}")
        age = time.time() - stat.st_mtime
    except OSError:
        INGEST_LOCK.unlink(missing_ok=True)
        return
    if age < max_sec:
        return
    log(f"kill stale ingest pid={pid} age_sec={age:.0f}")
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    time.sleep(2)
    try:
        os.kill(pid, 0)
        os.kill(pid, 9)
    except OSError:
        pass
    INGEST_LOCK.unlink(missing_ok=True)


def acquire_worker_lock() -> object | None:
    import fcntl

    WORKER_LOCK.parent.mkdir(parents=True, exist_ok=True)
    if WORKER_LOCK.exists():
        try:
            old_pid = int(WORKER_LOCK.read_text(encoding="utf-8").strip())
            os.kill(old_pid, 0)
        except ProcessLookupError:
            WORKER_LOCK.unlink(missing_ok=True)
        except (ValueError, OSError):
            WORKER_LOCK.unlink(missing_ok=True)
    handle = WORKER_LOCK.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        log("skip worker: another mlbb_continuous_worker is running")
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def hourly_status(base: dict[str, str]) -> None:
    token = base.get("TG_BOT_TOKEN", "")
    chat_id = base.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        from mlbb_calibration_store import stats

        s = stats()
        pending = pending_shorts()
        state_path = Path(os.environ.get("MLBB_CONTINUOUS_STATE", "/root/data/mlbb/mlbb_continuous_state.json"))
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        text = (
            f"📊 MLBB farm status\n"
            f"pending Shorts: {pending}\n"
            f"👍 {s.get('feedback_yes', 0)} 👎 {s.get('feedback_no', 0)}\n"
            f"worker cycles: {state.get('cycles', '?')}\n"
            f"ingest: {'on' if state.get('ingest_running') else 'off'} | "
            f"vod: {'on' if state.get('vod_running') else 'off'} | "
            f"feed: {'on' if state.get('feed_running') else 'off'}"
        )
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
            env={k: v for k, v in base.items() if "proxy" not in k.lower()},
            check=False,
            timeout=30,
        )
    except Exception as exc:
        log(f"hourly_status skipped: {exc}")


def pipeline_paused(name: str) -> bool:
    if os.environ.get("MLBB_SKIP_MONTAGE", "1") == "1" and name == "montage":
        return True
    if not PAUSED_PIPELINES.exists():
        return False
    paused = PAUSED_PIPELINES.read_text(encoding="utf-8")
    script = f"{name.replace('montage', 'mlbb_vod_montage_feed.py')}"
    if name == "montage":
        script = "mlbb_vod_montage_feed.py"
    return script in paused


def ingest_cmd(env: dict[str, str], *, aggressive: bool) -> list[str]:
    script = BIN / "mlbb_youtube_shorts_ingest.py"
    if not script.exists():
        script = Path(__file__).resolve().parent / "mlbb_youtube_shorts_ingest.py"
    burst = env.get("MLBB_SHORTS_CALIBRATION_BURST", "1") == "1"
    max_dl = env.get("MLBB_INGEST_MAX_DOWNLOADS", "8" if burst else "15")
    max_q = env.get("MLBB_INGEST_MAX_PER_QUERY", "40" if burst else "20")
    delay = env.get("MLBB_INGEST_DOWNLOAD_DELAY", "5" if burst else "8")
    return [
        PY,
        str(script),
        "--incremental",
        "--max-downloads",
        str(max_dl),
        "--max-per-query",
        str(max_q),
        "--download-delay",
        str(delay),
        "--search-delay",
        env.get("MLBB_INGEST_SEARCH_DELAY", "2" if burst else "3"),
    ]


def vod_cmd(env: dict[str, str]) -> list[str]:
    script = BIN / "mlbb_vod_segment_feed.py"
    if not script.exists():
        script = Path(__file__).resolve().parent / "mlbb_vod_segment_feed.py"
    return [PY, str(script)]


def feed_cmd(env: dict[str, str]) -> list[str]:
    script = BIN / "mlbb_calibration_feed.py"
    if not script.exists():
        script = Path(__file__).resolve().parent / "mlbb_calibration_feed.py"
    return [PY, str(script)]


def vod_env(base: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    if env.get("MLBB_SEND_ENABLED", "1") != "1":
        return env
    env.update(
        {
            "MLBB_VOD_SHORT_MODE": "1",
            "MLBB_VOD_MIN_SEC": env.get("MLBB_VOD_MIN_SEC", "900"),
            "MLBB_VOD_MAX_SEC": env.get("MLBB_VOD_MAX_SEC", "2700"),
            "MLBB_VOD_PIPELINE_MAX_MIN": env.get("MLBB_VOD_SLICE_MIN", "90"),
            "MLBB_VOD_PIPELINE_MAX_VODS": env.get("MLBB_VOD_SLICE_MAX_VODS", "8"),
            "MLBB_VOD_AUTO_DOWNLOAD": "1",
            "MLBB_VOD_PROBE_LIMIT": env.get("MLBB_VOD_PROBE_LIMIT", "40"),
            "MLBB_VOD_BATCH_MAX": env.get("MLBB_VOD_BATCH_MAX", "40"),
            "MLBB_VOD_SEGMENT_SEC": env.get("MLBB_VOD_SEGMENT_SEC", "15"),
            "MLBB_VOD_VARIABLE_LENGTH": "1",
            "MLBB_VOD_FULL_FRAME": "1",
            "SMART_CROP_WEBCAM": "0",
            "MLBB_VOD_KILL_FIRST": "1",
            "MLBB_VOD_CALIBRATION_LENIENT": env.get("MLBB_VOD_CALIBRATION_LENIENT", "1"),
            "MLBB_REQUIRE_KILL_UI": "1",
            "MLBB_FORCE_MAX_LIVE_VOD_SEC": "2700",
            "MLBB_FORCE_MAX_LIVE_VOD_FPS": "55",
            "MLBB_VOD_LEAD_SEC": "4",
            "HIGHLIGHT_WINDOW_SEC": env.get("HIGHLIGHT_WINDOW_SEC", "15"),
            "OWNER_PREVIEW_REQUIRED": "0",
            "LOGO_FILE": "/nonexistent/mlbb_calibration_no_logo.png",
            "YTDLP_SLEEP_REQUESTS": "1.5",
            "YTDLP_SLEEP_INTERVAL": "3",
            "YTDLP_MAX_SLEEP_INTERVAL": "10",
        }
    )
    return env


def ingest_env(base: dict[str, str], *, aggressive: bool) -> dict[str, str]:
    env = dict(base)
    env.update(
        {
            "MLBB_CALIBRATION_LENIENT": "1",
            "MLBB_SHORTS_CALIBRATION_BURST": "1" if aggressive else env.get("MLBB_SHORTS_CALIBRATION_BURST", "0"),
            "MLBB_SHORTS_STREAMER_ONLY": env.get("MLBB_SHORTS_STREAMER_ONLY", "0"),
            "MLBB_SHORTS_SEARCH_FALLBACK": env.get("MLBB_SHORTS_SEARCH_FALLBACK", "1"),
            "MLBB_SEARCH_BEFORE_STREAMERS": env.get("MLBB_SEARCH_BEFORE_STREAMERS", "1"),
            "MLBB_OWNER_CHANNELS_LAST": env.get("MLBB_OWNER_CHANNELS_LAST", "1"),
            "MLBB_OWNER_CHANNEL_LIMIT": env.get("MLBB_OWNER_CHANNEL_LIMIT", "2"),
            "MLBB_INGEST_FULL_SWEEP_PENDING": env.get("MLBB_INGEST_FULL_SWEEP_PENDING", "8"),
            "MLBB_INGEST_MAX_RUN_SEC": env.get("MLBB_INGEST_MAX_RUN_SEC", "2400"),
            "MLBB_STREAMER_REQUIRE_MLBB_TITLE": env.get("MLBB_STREAMER_REQUIRE_MLBB_TITLE", "1"),
                "MLBB_SHORTS_MIN_UPLOAD_DATE": env.get("MLBB_SHORTS_MIN_UPLOAD_DATE", "20260101"),
                "MLBB_SHORTS_MAX_DURATION_SEC": env.get("MLBB_SHORTS_MAX_DURATION_SEC", "1200"),
                "MLBB_SHORTS_INCLUDE_VIDEOS_TAB": env.get("MLBB_SHORTS_INCLUDE_VIDEOS_TAB", "1"),
            "MLBB_SHORTS_INGEST_DAYS": env.get("MLBB_SHORTS_INGEST_DAYS", "365"),
            "MLBB_TRAINING_ARCHIVE": env.get("MLBB_TRAINING_ARCHIVE", "1"),
            "MLBB_SHORTS_STRICT_VERIFY": env.get("MLBB_SHORTS_STRICT_VERIFY", "1"),
            "MLBB_SHORTS_REQUIRE_KILL_UI": env.get("MLBB_SHORTS_REQUIRE_KILL_UI", "1"),
            "MLBB_INGEST_SKIP_IF_PENDING": "0" if aggressive else env.get("MLBB_INGEST_SKIP_IF_PENDING", "999"),
            "YTDLP_SLEEP_REQUESTS": "1.2",
            "YTDLP_SLEEP_INTERVAL": "3",
            "YTDLP_MAX_SLEEP_INTERVAL": "10",
        }
    )
    return env


def write_state(state: dict) -> None:
    path = Path(os.environ.get("MLBB_CONTINUOUS_STATE", "/root/data/mlbb/mlbb_continuous_state.json"))
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def montage_cmd(env: dict[str, str]) -> list[str]:
    script = BIN / "mlbb_vod_montage_feed.py"
    if not script.exists():
        script = Path(__file__).resolve().parent / "mlbb_vod_montage_feed.py"
    return [PY, str(script)]


def main() -> int:
    base = base_env()
    target_pending = int(base.get("MLBB_TARGET_PENDING", "40"))
    vod_slice_min = int(base.get("MLBB_VOD_SLICE_MIN", "90"))
    vod_max_vods = int(base.get("MLBB_VOD_SLICE_MAX_VODS", "8"))
    ingest_cooldown = float(base.get("MLBB_INGEST_COOLDOWN_SEC", "180"))
    feed_cooldown = float(base.get("MLBB_FEED_COOLDOWN_SEC", "300"))
    vod_cooldown = float(base.get("MLBB_VOD_COOLDOWN_SEC", "300"))
    if base.get("MLBB_SEND_ENABLED", "1") != "1":
        log("MLBB_SEND_ENABLED=0 — worker idle (no Telegram sends)")
        return 0

    lock_handle = acquire_worker_lock()
    if lock_handle is None:
        return 0

    ingest = Proc("ingest", [], ingest_env(base, aggressive=True))
    vod = Proc("vod", vod_cmd(base), vod_env(base))
    feed = Proc("feed", feed_cmd(base), base)
    montage = Proc("montage", montage_cmd(base), base)
    montage_enabled = not pipeline_paused("montage")
    MONTAGE_COOLDOWN_SEC = float(os.environ.get("MLBB_MONTAGE_COOLDOWN_SEC", "7200"))

    log(
        f"mlbb_continuous_worker start pid={os.getpid()} target_pending={target_pending} "
        f"vod_slice={vod_slice_min}min batch={base.get('MLBB_CALIBRATION_BATCH')} "
        f"montage={'on' if montage_enabled else 'off'}"
    )
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    cycles = 0

    while True:
        try:
            cycles += 1
            pending = pending_shorts()
            aggressive = pending < target_pending

            if (
                aggressive
                and should_start_ingest(pending=pending, target_pending=target_pending)
                and not ingest.running()
                and not ingest_running_externally()
                and ingest.cooldown_ok(ingest_cooldown)
            ):
                kill_stale_ingest()
                ingest.cmd = ingest_cmd(base, aggressive=aggressive)
                ingest.env = ingest_env(base, aggressive=aggressive)
                ingest.start()

            if (
                should_start_vod(pending=pending)
                and not vod.running()
                and not vod_feed_running_externally()
                and vod.cooldown_ok(vod_cooldown)
            ):
                vod.start()

            feed_wait = feed_cooldown
            if pending > 0:
                feed_wait = float(base.get("MLBB_FEED_COOLDOWN_PENDING_SEC", "90"))
            if (
                pending > 0
                and not feed.running()
                and not feed_running_externally()
                and feed.cooldown_ok(feed_wait)
            ):
                feed.start()

            if montage_enabled and montage.cooldown_ok(MONTAGE_COOLDOWN_SEC):
                montage.start()

            for job in (ingest, vod, feed, montage):
                job.reap()

            if cycles % 900 == 0:
                hourly_status(base)

            if cycles % 90 == 0:
                sync_script = BIN / "mlbb_viral_threshold_sync.py"
                if not sync_script.exists():
                    sync_script = Path(__file__).resolve().parent / "mlbb_viral_threshold_sync.py"
                if sync_script.exists():
                    subprocess.run([PY, str(sync_script)], env=base, timeout=120, check=False)

            if cycles % 360 == 0:
                report_script = BIN / "mlbb_daily_report.py"
                if not report_script.exists():
                    report_script = Path(__file__).resolve().parent / "mlbb_daily_report.py"
                if report_script.exists():
                    subprocess.run([PY, str(report_script), "--telegram"], env=base, timeout=60, check=False)

            if cycles % 15 == 0:
                try:
                    from mlbb_calibration_store import repair_index

                    repair_index()
                except Exception as exc:
                    log(f"repair_index skipped: {exc}")
                write_state(
                    {
                        "pending_shorts": pending,
                        "ingest_running": ingest.running() or ingest_running_externally(),
                        "vod_running": vod.running() or vod_feed_running_externally(),
                        "feed_running": feed.running() or feed_running_externally(),
                        "cycles": cycles,
                        "worker_pid": os.getpid(),
                    }
                )
        except Exception as exc:
            log(f"loop error: {type(exc).__name__}: {exc}")

        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
