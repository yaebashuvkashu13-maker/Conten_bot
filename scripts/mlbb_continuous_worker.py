#!/usr/bin/env python3
"""MLBB 24/7 worker — ingest, VOD segment scan, calibration feed in parallel."""

from __future__ import annotations

import json
import os
import signal
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
_LAST_DISK_INDEX = 0.0
_LAST_EMPTY_FEED = 0.0
_LAST_STARVATION_INGEST = 0.0
_PENDING_ZERO_SINCE = 0.0
_LAST_DAILY_REPORT = 0.0
_LAST_HOURLY_STATUS = 0.0


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
            "MLBB_SHORTS_VERTICAL": env.get("MLBB_SHORTS_VERTICAL", "1"),
            "MLBB_PORTRAIT_RENDER": env.get("MLBB_PORTRAIT_RENDER", "1"),
            "MLBB_CALIBRATION_MIN_SEND_SCORE": env.get("MLBB_CALIBRATION_MIN_SEND_SCORE", "0"),
            "MLBB_CALIBRATION_BATCH": env.get("MLBB_CALIBRATION_BATCH", "4"),
            "MLBB_STEADY_MODE": env.get("MLBB_STEADY_MODE", "1"),
            "MLBB_SHORTS_CALIBRATION_BURST": env.get(
                "MLBB_SHORTS_CALIBRATION_BURST",
                "0" if env.get("MLBB_STEADY_MODE", "1") == "1" else "1",
            ),
            "HIGHLIGHT_HEATMAP": "0",
            "HIGHLIGHT_USE_OWNER_ANCHORS": "0",
            "YTDLP_REMOTE_COMPONENTS": env.get("YTDLP_REMOTE_COMPONENTS", "ejs:github"),
        }
    )
    path_parts = [
        env.get("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")),
        str(BIN),
        "/usr/local/bin",
        "/root/.deno/bin",
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for part in path_parts:
        for p in part.split(":"):
            p = p.strip()
            if p and p not in seen:
                seen.add(p)
                ordered.append(p)
    env["PATH"] = ":".join(ordered)
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

    def max_runtime_sec(self) -> float:
        defaults = {"ingest": 600.0, "feed": 420.0, "vod": 1200.0, "montage": 3600.0}
        key = f"MLBB_{self.name.upper()}_MAX_RUN_SEC"
        return float(self.env.get(key, os.environ.get(key, str(defaults.get(self.name, 600.0)))))

    def maybe_nudge_timeout(self) -> bool:
        if not self.running() or self.last_start <= 0:
            return False
        age = time.time() - self.last_start
        limit = self.max_runtime_sec()
        if age < limit:
            return False
        log(f"nudge timeout {self.name} pid={self.proc.pid} age_sec={age:.0f} max={limit:.0f}")
        self.stop()
        return True

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

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            self.proc = None
            return
        pid = self.proc.pid
        log(f"STOP {self.name} pid={pid}")
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            try:
                self.proc.terminate()
            except OSError:
                pass
        self.proc = None

    def cooldown_ok(self, sec: float) -> bool:
        if self.running():
            return False
        if self.last_start <= 0:
            return True
        return (time.time() - self.last_start) >= sec



def pending_shorts() -> int:
    global _LAST_INDEX_REBUILD, _LAST_DISK_INDEX
    from mlbb_calibration_store import index_unlabeled_disk_shorts, pending_candidates, rebuild_index_from_disk

    now = time.time()
    rebuild_interval = float(os.environ.get("MLBB_INDEX_REBUILD_SEC", "120"))
    if now - _LAST_INDEX_REBUILD >= rebuild_interval:
        try:
            rebuild_index_from_disk()
        except Exception as exc:
            log(f"rebuild_index_from_disk skipped: {exc}")
        _LAST_INDEX_REBUILD = now

    disk_interval = float(os.environ.get("MLBB_DISK_INDEX_SEC", "90"))
    if now - _LAST_DISK_INDEX >= disk_interval:
        try:
            added = index_unlabeled_disk_shorts(limit=int(os.environ.get("MLBB_RESCUE_LIMIT", "16")))
            if added:
                log(f"disk_index added={added}")
        except Exception as exc:
            log(f"disk_index skipped: {exc}")
        _LAST_DISK_INDEX = now

    try:
        return len(pending_candidates(limit=500, repair=False))
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
    if os.environ.get("MLBB_SHORTS_FOCUS", "0") == "1" and pending < target_pending:
        return True
    if os.environ.get("MLBB_ONE_HEAVY_JOB", "1") != "1":
        return True
    if not vod_feed_running_externally():
        return True
    return pending < int(os.environ.get("MLBB_INGEST_FORCE_PENDING", "3"))


def should_start_vod(*, pending: int, target_pending: int, base: dict[str, str] | None = None) -> bool:
    cfg = base or load_env_file()
    if cfg.get("MLBB_VOD_PARALLEL", "0") != "1":
        if cfg.get("MLBB_SHORTS_FOCUS", "0") == "1" and pending < target_pending:
            return False
    elif cfg.get("MLBB_VOD_DISABLED", "0") == "1":
        return False
    if cfg.get("MLBB_ONE_HEAVY_JOB", os.environ.get("MLBB_ONE_HEAVY_JOB", "1")) != "1":
        return True
    if not ingest_running_externally():
        return True
    pause_at = int(cfg.get("MLBB_VOD_PAUSE_WHEN_SHORTS_PENDING", "6"))
    if cfg.get("MLBB_VOD_PARALLEL", "0") == "1":
        pause_at = int(cfg.get("MLBB_VOD_PARALLEL_MIN_PENDING", "0"))
    return pending >= pause_at


def kill_stale_lock(lock_path: Path, *, name: str, max_age_sec: float) -> None:
    """Kill process holding lock if lock file is older than max_age_sec."""
    if not lock_path.exists():
        return
    try:
        pid = int(lock_path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError, OSError):
        lock_path.unlink(missing_ok=True)
        return
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        lock_path.unlink(missing_ok=True)
        return
    if age < max_age_sec:
        return
    log(f"kill stale {name} pid={pid} lock_age_sec={age:.0f}")
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    time.sleep(1)
    try:
        os.kill(pid, 0)
        os.kill(pid, 9)
    except OSError:
        pass
    lock_path.unlink(missing_ok=True)


def kill_orphan_ingest() -> None:
    """Kill duplicate ingest workers — only the lock holder should run."""
    holder: int | None = None
    if INGEST_LOCK.exists():
        try:
            holder = int(INGEST_LOCK.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            holder = None
    try:
        out = subprocess.run(
            ["pgrep", "-f", "mlbb_youtube_shorts_ingest.py"],
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
            log(f"kill orphan ingest pid={pid}")
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    except Exception:
        pass


def kill_stale_ingest() -> None:
    """Free ingest lock if a previous run hung (blocks worker for hours)."""
    try:
        from mlbb_job_watchdog import nudge_lock, kill_orphans

        nudge_lock(
            "ingest",
            INGEST_LOCK,
            "mlbb_youtube_shorts_ingest.py",
            float(os.environ.get("MLBB_INGEST_STALE_SEC", "900")),
        )
        kill_orphans("mlbb_youtube_shorts_ingest.py", INGEST_LOCK)
    except ImportError:
        kill_orphan_ingest()
        stale_sec = float(os.environ.get("MLBB_INGEST_STALE_SEC", "900"))
        kill_stale_lock(INGEST_LOCK, name="ingest", max_age_sec=stale_sec)


def kill_stale_feed() -> None:
    try:
        from mlbb_job_watchdog import nudge_lock

        nudge_lock(
            "feed",
            FEED_LOCK,
            "mlbb_calibration_feed.py",
            float(os.environ.get("MLBB_FEED_STALE_SEC", "300")),
        )
    except ImportError:
        stale_sec = float(os.environ.get("MLBB_FEED_STALE_SEC", "300"))
        kill_stale_lock(FEED_LOCK, name="feed", max_age_sec=stale_sec)


def nudge_stale_jobs() -> None:
    try:
        from mlbb_job_watchdog import nudge_all

        acts = nudge_all()
        if acts:
            log("nudge " + " ".join(acts))
    except Exception as exc:
        log(f"nudge_stale_jobs skipped: {exc}")


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
    if name == "ingest":
        script = "mlbb_youtube_shorts_ingest.py"
    elif name == "feed":
        script = "mlbb_calibration_feed.py"
    elif name == "vod":
        script = "mlbb_vod_segment_feed.py"
    elif name == "montage":
        script = "mlbb_vod_montage_feed.py"
    else:
        script = name
    return script in paused


def ingest_cmd(env: dict[str, str], *, aggressive: bool, steady: bool = False) -> list[str]:
    script = BIN / "mlbb_youtube_shorts_ingest.py"
    if not script.exists():
        script = Path(__file__).resolve().parent / "mlbb_youtube_shorts_ingest.py"
    burst = env.get("MLBB_SHORTS_CALIBRATION_BURST", "0") == "1" and not steady
    if steady:
        max_dl = env.get("MLBB_STEADY_INGEST_MAX_DOWNLOADS", "4")
        max_q = env.get("MLBB_STEADY_INGEST_MAX_PER_QUERY", "20")
        delay = env.get("MLBB_STEADY_INGEST_DOWNLOAD_DELAY", "4")
    else:
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
        env.get("MLBB_STEADY_INGEST_SEARCH_DELAY" if steady else "MLBB_INGEST_SEARCH_DELAY", "5" if steady else "2" if burst else "3"),
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
            "MLBB_VOD_MIN_SEC": env.get("MLBB_VOD_MIN_SEC", "300"),
            "MLBB_VOD_MAX_SEC": env.get("MLBB_VOD_MAX_SEC", "1200"),
            "MLBB_VOD_TARGET_DUR_SEC": env.get("MLBB_VOD_TARGET_DUR_SEC", "600"),
            "MLBB_VOD_PIPELINE_MAX_MIN": env.get("MLBB_VOD_SLICE_MIN", "120"),
            "MLBB_VOD_PIPELINE_MAX_VODS": env.get("MLBB_VOD_SLICE_MAX_VODS", "4"),
            "MLBB_VOD_AUTO_DOWNLOAD": "1",
            "MLBB_VOD_PROBE_LIMIT": env.get("MLBB_VOD_PROBE_LIMIT", "24"),
            "MLBB_VOD_BATCH_MAX": env.get("MLBB_VOD_BATCH_MAX", "4"),
            "MLBB_VOD_MAX_CLIPS_PER_RUN": env.get("MLBB_VOD_MAX_CLIPS_PER_RUN", "15"),
            "MLBB_VOD_MAX_RUN_SEC": env.get("MLBB_VOD_MAX_RUN_SEC", "3600"),
            "MLBB_VOD_SEGMENT_SEC": env.get("MLBB_VOD_SEGMENT_SEC", "15"),
            "MLBB_VOD_VARIABLE_LENGTH": "1",
            "MLBB_VOD_FULL_FRAME": "1",
            "SMART_CROP_WEBCAM": "0",
            "MLBB_VOD_KILL_FIRST": "1",
            "MLBB_VOD_CALIBRATION_LENIENT": env.get("MLBB_VOD_CALIBRATION_LENIENT", "0"),
            "MLBB_FIGHT_UNTIL_END": env.get("MLBB_FIGHT_UNTIL_END", "1"),
            "MLBB_FIGHT_MIN_SEC": env.get("MLBB_FIGHT_MIN_SEC", "10"),
            "MLBB_FIGHT_MAX_SEC": env.get("MLBB_FIGHT_MAX_SEC", "90"),
            "MLBB_VOD_LEAD_SEC": env.get("MLBB_VOD_LEAD_SEC", "4"),
            "MLBB_VOD_MIN_PEAK_SEC": env.get("MLBB_VOD_MIN_PEAK_SEC", "120"),
            "MLBB_REQUIRE_MULTIKILL": env.get("MLBB_REQUIRE_MULTIKILL", "1"),
            "MLBB_KILL_SCAN_SKIP_OCR": env.get("MLBB_KILL_SCAN_SKIP_OCR", "0"),
            "MLBB_KILL_SCAN_STEP_SEC": env.get("MLBB_KILL_SCAN_STEP_SEC", "30"),
            "MLBB_VOD_SIMPLE_RENDER_MIN_SEC": env.get("MLBB_VOD_SIMPLE_RENDER_MIN_SEC", "18"),
            "MLBB_REQUIRE_KILL_UI": "1",
            "MLBB_FORCE_MAX_LIVE_VOD_SEC": "2700",
            "MLBB_FORCE_MAX_LIVE_VOD_FPS": "55",
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
            "MLBB_SHORTS_ONLY": env.get("MLBB_SHORTS_ONLY", "1" if aggressive else "0"),
            "MLBB_SHORTS_FOCUS": env.get("MLBB_SHORTS_FOCUS", "1" if aggressive else "0"),
            "MLBB_SHORTS_SEARCH_FALLBACK": env.get("MLBB_SHORTS_SEARCH_FALLBACK", "1"),
            "MLBB_SEARCH_BEFORE_STREAMERS": env.get("MLBB_SEARCH_BEFORE_STREAMERS", "1"),
            "MLBB_OWNER_CHANNELS_LAST": env.get("MLBB_OWNER_CHANNELS_LAST", "1"),
            "MLBB_OWNER_CHANNEL_LIMIT": env.get("MLBB_OWNER_CHANNEL_LIMIT", "2"),
            "MLBB_INGEST_FULL_SWEEP_PENDING": env.get("MLBB_INGEST_FULL_SWEEP_PENDING", "8"),
            "MLBB_INGEST_MAX_RUN_SEC": env.get("MLBB_INGEST_MAX_RUN_SEC", "900"),
            "MLBB_STREAMER_REQUIRE_MLBB_TITLE": env.get("MLBB_STREAMER_REQUIRE_MLBB_TITLE", "1"),
                "MLBB_SHORTS_MIN_UPLOAD_DATE": env.get("MLBB_SHORTS_MIN_UPLOAD_DATE", "20260101"),
                "MLBB_SHORTS_MAX_DURATION_SEC": env.get("MLBB_SHORTS_MAX_DURATION_SEC", "1200"),
                "MLBB_SHORTS_INCLUDE_VIDEOS_TAB": env.get("MLBB_SHORTS_INCLUDE_VIDEOS_TAB", "1"),
            "MLBB_SHORTS_INGEST_DAYS": env.get("MLBB_SHORTS_INGEST_DAYS", "365"),
            "MLBB_TRAINING_ARCHIVE": env.get("MLBB_TRAINING_ARCHIVE", "1"),
            "MLBB_SHORTS_STRICT_VERIFY": env.get("MLBB_SHORTS_STRICT_VERIFY", "1"),
            "MLBB_SHORTS_REQUIRE_KILL_UI": env.get("MLBB_SHORTS_REQUIRE_KILL_UI", "1"),
            "MLBB_INGEST_SKIP_IF_PENDING": "0" if aggressive else env.get("MLBB_INGEST_SKIP_IF_PENDING", "999"),
            "YTDLP_SLEEP_REQUESTS": "0.5" if aggressive else "1.2",
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
    target_pending = int(base.get("MLBB_TARGET_PENDING", "12"))
    vod_slice_min = int(base.get("MLBB_VOD_SLICE_MIN", "90"))
    vod_max_vods = int(base.get("MLBB_VOD_SLICE_MAX_VODS", "8"))
    base_ingest_cooldown = float(base.get("MLBB_INGEST_COOLDOWN_SEC", "180"))
    base_feed_cooldown = float(base.get("MLBB_FEED_COOLDOWN_SEC", "300"))
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
    last_tier = -1
    global _LAST_EMPTY_FEED, _LAST_STARVATION_INGEST, _PENDING_ZERO_SINCE

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
            now = time.time()
            starve_pending = int(os.environ.get("MLBB_STARVATION_PENDING", "3"))
            if pending < starve_pending:
                if _PENDING_ZERO_SINCE <= 0:
                    _PENDING_ZERO_SINCE = now
            else:
                _PENDING_ZERO_SINCE = 0.0

            try:
                from mlbb_calibration_tier import apply_tier

                tier, tier_env = apply_tier(base, pending=pending)
                if base.get("MLBB_STEADY_MODE", "1") == "1":
                    tier = min(tier, int(base.get("MLBB_STEADY_MAX_TIER", "2")))
                    from mlbb_calibration_tier import tier_env as _tier_env

                    tier_env = {**base, **_tier_env(tier), "MLBB_CALIBRATION_TIER": str(tier)}
            except ImportError:
                tier, tier_env = 1, dict(base)
            if tier != last_tier:
                log(f"calibration tier={tier} pending={pending}")
                last_tier = tier
            ingest_cooldown = float(tier_env.get("MLBB_INGEST_COOLDOWN_SEC", base_ingest_cooldown))
            feed_cooldown = float(tier_env.get("MLBB_FEED_COOLDOWN_SEC", base_feed_cooldown))
            vod.env = {**vod_env(base), **tier_env}
            steady = base.get("MLBB_STEADY_MODE", "1") == "1"
            if steady:
                ingest_cooldown = float(base.get("MLBB_STEADY_INGEST_COOLDOWN_SEC", "300"))

            starved_for = (now - _PENDING_ZERO_SINCE) if _PENDING_ZERO_SINCE > 0 else 0.0
            steady_starve_sec = float(os.environ.get("MLBB_STEADY_STARVATION_SEC", "60"))
            starvation = pending < starve_pending and (
                not steady or starved_for >= steady_starve_sec or pending == 0
            )
            force_ingest = False
            starve_ingest_sec = float(os.environ.get("MLBB_STARVATION_INGEST_SEC", "120"))
            if starvation and starved_for >= starve_ingest_sec:
                if now - _LAST_STARVATION_INGEST >= starve_ingest_sec:
                    if not ingest.running() and not ingest_running_externally():
                        force_ingest = True
                        log(f"starvation ingest pending={pending} starved_for={starved_for:.0f}s")
                    else:
                        _LAST_STARVATION_INGEST = now

            refill_pending = pending < target_pending
            vod_focus = base.get("MLBB_VOD_FOCUS", "0") == "1"
            if vod_focus and base.get("MLBB_SHORTS_INGEST_DURING_VOD", "0") != "1":
                refill_pending = False
            if (
                (refill_pending or force_ingest)
                and should_start_ingest(pending=pending, target_pending=target_pending)
                and not ingest.running()
                and not ingest_running_externally()
                and (ingest.cooldown_ok(ingest_cooldown) or force_ingest)
            ):
                kill_stale_ingest()
                ingest.cmd = ingest_cmd(
                    {**base, **tier_env},
                    aggressive=refill_pending and not steady,
                    steady=steady,
                )
                ingest_env_map = {
                    **ingest_env(base, aggressive=refill_pending and not steady),
                    **tier_env,
                }
                if steady:
                    ingest_env_map["MLBB_SHORTS_CALIBRATION_BURST"] = "0"
                    ingest_env_map["MLBB_INGEST_SKIP_IF_PENDING"] = "0"
                if pending == 0 or pending < starve_pending:
                    ingest_env_map["MLBB_STARVATION_INGEST"] = "1"
                    ingest_env_map["MLBB_SHORTS_REQUIRE_KILL_UI"] = "0"
                    ingest_env_map["MLBB_INGEST_SKIP_IF_PENDING"] = "0"
                    ingest_env_map["MLBB_SHORTS_SKIP_DATE_FILTER"] = "1"
                if force_ingest:
                    ingest_env_map["MLBB_STARVATION_INGEST"] = "1"
                    ingest_env_map["MLBB_INGEST_SKIP_IF_PENDING"] = "0"
                    if steady:
                        ingest_env_map["MLBB_SHORTS_CALIBRATION_BURST"] = "0"
                    _LAST_STARVATION_INGEST = now
                ingest.env = ingest_env_map
                ingest.start()

            vod_parallel = base.get("MLBB_VOD_PARALLEL", tier_env.get("MLBB_VOD_PARALLEL", "0")) == "1"
            shorts_focus = base.get("MLBB_SHORTS_FOCUS", tier_env.get("MLBB_SHORTS_FOCUS", "0")) == "1"
            if (
                base.get("MLBB_VOD_DISABLED", "0") != "1"
                and (not shorts_focus or vod_parallel)
                and should_start_vod(pending=pending, target_pending=target_pending, base=base)
                and not vod.running()
                and not vod_feed_running_externally()
                and vod.cooldown_ok(vod_cooldown)
            ):
                vod.start()
            elif (
                shorts_focus
                and not vod_parallel
                and pending < target_pending
                and (vod.running() or vod_feed_running_externally())
            ):
                if vod.running():
                    vod.stop()
                kill_stale_lock(VOD_LOCK, name="vod_shorts_focus", max_age_sec=0)

            feed_wait = feed_cooldown
            batch_size = int(base.get("MLBB_CALIBRATION_BATCH", "4"))
            feed_allowed = True
            feed_block_reason = ""
            vod_focus = base.get("MLBB_VOD_FOCUS", "0") == "1"
            if vod_focus and base.get("MLBB_SHORTS_FEED_DURING_VOD", "0") != "1":
                feed_allowed = False
                feed_block_reason = "vod_focus"
            elif vod.running() or vod_feed_running_externally():
                feed_allowed = False
                feed_block_reason = "vod_active"
            elif steady:
                try:
                    from mlbb_pipeline_health import should_send_feed_steady

                    feed_allowed, feed_block_reason = should_send_feed_steady(
                        pending=pending,
                        batch_size=batch_size,
                    )
                except ImportError:
                    pass
            if pending > 0 and not steady:
                feed_wait = float(
                    tier_env.get(
                        "MLBB_FEED_COOLDOWN_PENDING_SEC",
                        base.get("MLBB_FEED_COOLDOWN_PENDING_SEC", "60"),
                    )
                )
                if starvation:
                    feed_wait = min(feed_wait, 45.0)
            empty_feed_sec = float(os.environ.get("MLBB_FEED_EMPTY_RUN_SEC", "120"))
            if starvation:
                empty_feed_sec = min(empty_feed_sec, 45.0)
            run_feed = (pending > 0 and feed_allowed) or (
                pending == 0
                and feed_allowed
                and (time.time() - _LAST_EMPTY_FEED >= empty_feed_sec)
            )
            if pending > 0 and steady and not feed_allowed:
                if cycles % 30 == 0:
                    log(f"steady feed wait: {feed_block_reason} pending={pending}")
            if (
                run_feed
                and not feed.running()
                and not feed_running_externally()
                and feed.cooldown_ok(30 if steady else (feed_wait if pending > 0 else 30))
            ):
                kill_stale_feed()
                feed_env = {**base, **tier_env}
                if pending == 0:
                    feed_env["MLBB_FEED_REBUILD"] = "1"
                if starvation:
                    feed_env["MLBB_FEED_REBUILD"] = "1"
                    feed_env["MLBB_DISK_INDEX_LIMIT"] = os.environ.get("MLBB_STARVATION_DISK_LIMIT", "32")
                feed.env = feed_env
                feed.start()
                if pending == 0:
                    _LAST_EMPTY_FEED = time.time()

            if montage_enabled and montage.cooldown_ok(MONTAGE_COOLDOWN_SEC):
                montage.start()

            for job in (ingest, vod, feed, montage):
                job.maybe_nudge_timeout()
                job.reap()

            if cycles % 30 == 0:
                nudge_stale_jobs()

            global _LAST_HOURLY_STATUS, _LAST_DAILY_REPORT
            hourly_interval = float(base.get("MLBB_HOURLY_STATUS_INTERVAL_SEC", "3600"))
            if base.get("MLBB_HOURLY_STATUS_TELEGRAM", "0") == "1":
                if now - _LAST_HOURLY_STATUS >= hourly_interval:
                    _LAST_HOURLY_STATUS = now
                    hourly_status(base)

            if cycles % 90 == 0:
                sync_script = BIN / "mlbb_viral_threshold_sync.py"
                if not sync_script.exists():
                    sync_script = Path(__file__).resolve().parent / "mlbb_viral_threshold_sync.py"
                if sync_script.exists():
                    subprocess.run([PY, str(sync_script)], env=base, timeout=120, check=False)

            daily_interval = float(base.get("MLBB_DAILY_REPORT_INTERVAL_SEC", "86400"))
            if base.get("MLBB_DAILY_REPORT_TELEGRAM", "0") == "1":
                if now - _LAST_DAILY_REPORT >= daily_interval:
                    _LAST_DAILY_REPORT = now
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
                        "starved_for_sec": int(starved_for) if starvation else 0,
                        "steady_mode": steady,
                        "feed_block_reason": feed_block_reason if steady else "",
                        "calibration_tier": tier,
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
