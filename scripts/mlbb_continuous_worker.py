#!/usr/bin/env python3
"""MLBB 24/7 worker — YouTube Shorts ingest + calibration feed (optional VOD).

Default with MLBB_VOD_DISABLED=1: fresh Shorts only (~15/hour to Telegram).
VOD/montage stay off unless explicitly re-enabled on the server.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import fcntl
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LOG = Path(os.environ.get("MLBB_CONTINUOUS_LOG", "/root/data/mlbb/mlbb_continuous_worker.log"))
ENV_FILE = Path("/root/.video_bot.env")
BIN = Path("/usr/local/bin")
PY = sys.executable

# How many unevaluated Shorts we want queued for owner 👍/👎
TARGET_PENDING = int(os.environ.get("MLBB_TARGET_PENDING", "25"))
LOOP_SEC = float(os.environ.get("MLBB_CONTINUOUS_LOOP_SEC", "4"))
INGEST_COOLDOWN_SEC = float(os.environ.get("MLBB_INGEST_COOLDOWN_SEC", "120"))
FEED_COOLDOWN_SEC = float(os.environ.get("MLBB_FEED_COOLDOWN_SEC", "720"))
VOD_SLICE_MIN = int(os.environ.get("MLBB_VOD_SLICE_MIN", "30"))
VOD_MAX_VODS = int(os.environ.get("MLBB_VOD_SLICE_MAX_VODS", "2"))
VOD_DISABLED = os.environ.get("MLBB_VOD_DISABLED", "1") == "1"
ONE_HEAVY_JOB = os.environ.get("MLBB_ONE_HEAVY_JOB", "0" if VOD_DISABLED else "1") == "1"
CALIBRATION_FEED = os.environ.get(
    "MLBB_CALIBRATION_FEED_ENABLED", "1" if VOD_DISABLED else "0"
) == "1"
SHORTS_DAYS = int(os.environ.get("MLBB_SHORTS_DAYS", "365"))
VOD_STALE_SEC = float(os.environ.get("MLBB_VOD_STALE_SEC", "2700"))  # 45 min
INGEST_STALE_SEC = float(os.environ.get("MLBB_INGEST_STALE_SEC", "2400"))  # 40 min
HERO_MONTAGE_STALE_SEC = float(os.environ.get("MLBB_HERO_MONTAGE_STALE_SEC", "1200"))  # 20 min
ONEOFF_LOCK = Path("/tmp/mlbb_vod_oneoff.lock")


def oneoff_running() -> bool:
    return ONEOFF_LOCK.exists()


def calibration_feed_running() -> bool:
    proc = subprocess.run(
        ["pgrep", "-f", "mlbb_calibration_feed.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())


def ingest_pids() -> list[int]:
    proc = subprocess.run(
        ["pgrep", "-f", "mlbb_youtube_shorts_ingest.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    out: list[int] = []
    for line in (proc.stdout or "").splitlines():
        try:
            out.append(int(line.strip()))
        except ValueError:
            continue
    return out


def ingest_running_global() -> bool:
    return bool(ingest_pids())


def prune_duplicate_ingests(*, keep_pid: int | None = None) -> int:
    """Kill zombie ingests — main cause of load 50+ and zero throughput."""
    pids = ingest_pids()
    if len(pids) <= 1:
        return 0
    pids.sort()
    keep = keep_pid if keep_pid in pids else pids[-1]
    killed = 0
    for pid in pids:
        if pid == keep:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except OSError:
            pass
    if killed:
        time.sleep(1)
    return killed


def vod_feed_pids() -> list[int]:
    proc = subprocess.run(
        ["pgrep", "-f", "mlbb_vod_segment_feed.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    mine = os.getpid()
    out: list[int] = []
    for line in (proc.stdout or "").splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid != mine:
            out.append(pid)
    return out


def _cpu_cores() -> int:
    try:
        return os.cpu_count() or 4
    except OSError:
        return 4


def kill_orphan_heavy_procs() -> None:
    """Drop CLIP-heavy children left from crashed workers — not our own subprocesses."""
    if os.environ.get("MLBB_VOD_DISABLED", "1") == "1":
        patterns = [
            "mlbb_vod_segment_feed.py",
            "mlbb_vod_montage_feed.py",
            "mlbb_vod_oneoff.py",
        ]
    else:
        patterns = [
            "mlbb_youtube_shorts_ingest.py",
            "mlbb_vod_segment_feed.py",
            "mlbb_vod_montage_feed.py",
        ]
    for pat in patterns:
        subprocess.run(["pkill", "-f", pat], check=False)
    if os.environ.get("MLBB_VOD_DISABLED", "1") == "1":
        ONEOFF_LOCK.unlink(missing_ok=True)


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
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key.startswith("MLBB_") or key in ("TG_BOT_TOKEN", "TG_CHAT_ID"):
            if val:
                env[key] = val
        else:
            env.setdefault(key, val)
    return env


def base_env() -> dict[str, str]:
    env = load_env_file()
    env.update(
        {
            "CONTENT_BOT_REPO": env.get("CONTENT_BOT_REPO", "/root/content_bot_ml"),
            "MLBB_DATA_ROOT": env.get("MLBB_DATA_ROOT", "/root/data/mlbb"),
            "PYTHONPATH": f"{BIN}:{env.get('CONTENT_BOT_REPO', '/root/content_bot_ml')}/scripts",
            "MLBB_ONLY_MODE": "1",
            "MLBB_LEARNING_FIRST": "0",
            "MLBB_SEND_ENABLED": "1",
            "MLBB_VOD_VARIABLE_LENGTH": "1",
            "MLBB_VOD_LEAD_SEC": "4",
            "MLBB_MAX_DAILY_SENDS": env.get("MLBB_MAX_DAILY_SENDS", "150"),
            "MLBB_VOD_BATCH_MAX": env.get("MLBB_VOD_BATCH_MAX", "30"),
            "MLBB_CALIBRATION_BATCH": env.get("MLBB_CALIBRATION_BATCH", "6"),
            "MLBB_SHORTS_DAYS": env.get("MLBB_SHORTS_DAYS", "365"),
            "MLBB_SHORTS_MIN_YEAR": env.get("MLBB_SHORTS_MIN_YEAR", "2025"),
            "MLBB_SHORTS_YEAR_ONLY": env.get("MLBB_SHORTS_YEAR_ONLY", "1"),
            "HIGHLIGHT_OWNER_BAD_PAD_SEC": env.get("HIGHLIGHT_OWNER_BAD_PAD_SEC", "90"),
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
        self.last_finish = 0.0

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def age_sec(self) -> float:
        if not self.running():
            return 0.0
        return time.time() - self.last_start

    def stop(self, *, reason: str = "") -> None:
        if not self.proc or self.proc.poll() is not None:
            self.proc = None
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except OSError:
            pass
        log(f"STOP {self.name} reason={reason}")
        self.proc = None
        self.last_finish = time.time()

    def start(self) -> bool:
        self.reap()
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
        self.last_finish = time.time()
        return rc

    def gap_ok(self, min_gap: float) -> bool:
        """Ready for next job after min_gap seconds since last finish (not fixed schedule)."""
        if self.running():
            return False
        if self.last_finish <= 0:
            return True
        return (time.time() - self.last_finish) >= min_gap

    def cooldown_ok(self, sec: float) -> bool:
        return self.gap_ok(sec)


def pending_shorts() -> int:
    from mlbb_calibration_store import pending_candidates, rebuild_index_from_disk

    return len(pending_candidates(limit=9999))


_PENDING_CACHE: tuple[float, int] | None = None
_REBUILD_CACHE: float = 0.0


def _invalidate_pending_cache() -> None:
    global _PENDING_CACHE
    _PENDING_CACHE = None


def _cached_pending_shorts(*, now: float | None = None) -> int:
    """Throttle expensive index scans — worker polls every few seconds."""
    global _PENDING_CACHE, _REBUILD_CACHE
    ts = now if now is not None else time.time()
    rebuild_every = float(os.environ.get("MLBB_REBUILD_INDEX_SEC", "90"))
    pending_every = float(os.environ.get("MLBB_PENDING_CACHE_SEC", "20"))
    if ts - _REBUILD_CACHE >= rebuild_every:
        from mlbb_calibration_store import rebuild_index_from_disk

        rebuild_index_from_disk()
        _REBUILD_CACHE = ts
        _PENDING_CACHE = None
    if _PENDING_CACHE and ts - _PENDING_CACHE[0] < pending_every:
        return _PENDING_CACHE[1]
    count = pending_shorts()
    _PENDING_CACHE = (ts, count)
    return count


def ingest_cmd(env: dict[str, str], *, aggressive: bool, hungry: bool = False) -> list[str]:
    script = BIN / "mlbb_youtube_shorts_ingest.py"
    if not script.exists():
        script = Path(__file__).resolve().parent / "mlbb_youtube_shorts_ingest.py"
    refill = aggressive or hungry
    max_dl = "20" if refill else "6"
    max_q = "30" if refill else "12"
    days = env.get("MLBB_SHORTS_DAYS", "365")
    return [
        PY,
        str(script),
        "--incremental",
        "--days",
        days,
        "--max-downloads",
        max_dl,
        "--max-per-query",
        max_q,
        "--download-delay",
        "6",
        "--search-delay",
        "2",
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
    cores = _cpu_cores()
    probe = str(min(20, max(10, cores * 4)))
    pann = str(min(24, max(12, cores * 5)))
    stage1 = str(min(40, max(24, cores * 8)))
    env = dict(base)
    env.update(
        {
            "MLBB_VOD_SHORT_MODE": "1",
            "MLBB_VOD_MIN_SEC": env.get("MLBB_VOD_MIN_SEC", "900"),
            "MLBB_VOD_MAX_SEC": env.get("MLBB_VOD_MAX_SEC", "2700"),
            "MLBB_VOD_PIPELINE_MAX_MIN": str(VOD_SLICE_MIN),
            "MLBB_VOD_PIPELINE_MAX_VODS": str(VOD_MAX_VODS),
            "MLBB_VOD_AUTO_DOWNLOAD": "1",
            "MLBB_VOD_PROBE_LIMIT": env.get("MLBB_VOD_PROBE_LIMIT", probe),
            "MLBB_VOD_BATCH_MAX": env.get("MLBB_VOD_BATCH_MAX", "5"),
            "MLBB_VOD_SEGMENT_SEC": env.get("MLBB_VOD_SEGMENT_SEC", "15"),
            "MLBB_VOD_VARIABLE_LENGTH": "1",
            "MLBB_VOD_LEAD_SEC": "4",
            "HIGHLIGHT_WINDOW_SEC": env.get("HIGHLIGHT_WINDOW_SEC", "15"),
            "HIGHLIGHT_MAX_PANN_PROBE": env.get("HIGHLIGHT_MAX_PANN_PROBE", pann),
            "HIGHLIGHT_MAX_STAGE1": env.get("HIGHLIGHT_MAX_STAGE1", stage1),
            "OWNER_PREVIEW_REQUIRED": "0",
            "LOGO_FILE": "/nonexistent/mlbb_calibration_no_logo.png",
            "YTDLP_SLEEP_REQUESTS": "1.5",
            "YTDLP_SLEEP_INTERVAL": "3",
            "YTDLP_MAX_SLEEP_INTERVAL": "10",
        }
    )
    return env


def ingest_env(base: dict[str, str], *, aggressive: bool, hungry: bool = False) -> dict[str, str]:
    env = dict(base)
    refill = aggressive or hungry
    env.update(
        {
            "MLBB_CALIBRATION_LENIENT": "1",
            "MLBB_CALIBRATION_FAST_INGEST": env.get("MLBB_CALIBRATION_FAST_INGEST", "0"),
            "MLBB_SHORTS_DAYS": env.get("MLBB_SHORTS_DAYS", "365"),
            "MLBB_SHORTS_MIN_YEAR": env.get("MLBB_SHORTS_MIN_YEAR", "2025"),
            "MLBB_SHORTS_YEAR_ONLY": env.get("MLBB_SHORTS_YEAR_ONLY", "1"),
            "MLBB_INGEST_SKIP_IF_PENDING": "0" if refill else env.get("MLBB_INGEST_SKIP_IF_PENDING", "6"),
            "MLBB_INGEST_HUNGRY": "1" if hungry else ("1" if aggressive else env.get("MLBB_INGEST_HUNGRY", "0")),
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


def hero_montages_today() -> int:
    path = Path(os.environ.get("MLBB_HERO_SHORTS_MONTAGE_STATE", "/root/data/mlbb/hero_shorts_montage.json"))
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    today = time.strftime("%Y-%m-%d")
    return sum(1 for row in data.get("runs", []) if str(row.get("at", "")).startswith(today))


def hero_shorts_montage_cmd(env: dict[str, str]) -> list[str]:
    script = BIN / "mlbb_hero_shorts_montage.py"
    if not script.exists():
        script = Path(__file__).resolve().parent / "mlbb_hero_shorts_montage.py"
    return [PY, str(script)]


def montage_cmd(env: dict[str, str]) -> list[str]:
    script = BIN / "mlbb_vod_montage_feed.py"
    if not script.exists():
        script = Path(__file__).resolve().parent / "mlbb_vod_montage_feed.py"
    return [PY, str(script)]


def main() -> int:
    lock_path = Path("/tmp/mlbb_continuous_worker.lock")
    lock_fd = lock_path.open("w")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another mlbb_continuous_worker running", file=sys.stderr)
        return 0

    base = base_env()
    os.environ.update({k: v for k, v in base.items() if v})
    global FEED_COOLDOWN_SEC, INGEST_COOLDOWN_SEC, TARGET_PENDING, CALIBRATION_FEED, VOD_DISABLED, SHORTS_DAYS, ONE_HEAVY_JOB
    FEED_COOLDOWN_SEC = float(base.get("MLBB_FEED_COOLDOWN_SEC", str(FEED_COOLDOWN_SEC)))
    INGEST_COOLDOWN_SEC = float(base.get("MLBB_INGEST_COOLDOWN_SEC", str(INGEST_COOLDOWN_SEC)))
    TARGET_PENDING = int(base.get("MLBB_TARGET_PENDING", str(TARGET_PENDING)))
    CALIBRATION_FEED = base.get("MLBB_CALIBRATION_FEED_ENABLED", "1" if VOD_DISABLED else "0") == "1"
    VOD_DISABLED = base.get("MLBB_VOD_DISABLED", "1") == "1"
    ONE_HEAVY_JOB = base.get("MLBB_ONE_HEAVY_JOB", "0" if VOD_DISABLED else "1") == "1"
    SHORTS_DAYS = int(base.get("MLBB_SHORTS_DAYS", "365"))
    PIDFILE = Path(os.environ.get("MLBB_CONTINUOUS_PID", "/root/data/mlbb/mlbb_continuous_worker.pid"))
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    kill_orphan_heavy_procs()
    prune_duplicate_ingests()
    time.sleep(2)
    ingest = Proc("ingest", [], ingest_env(base, aggressive=True))
    vod = Proc("vod", vod_cmd(base), vod_env(base))
    feed = Proc("feed", feed_cmd(base), base)
    montage = Proc("montage", montage_cmd(base), base)
    hero_montage = Proc("hero_montage", hero_shorts_montage_cmd(base), base)
    MONTAGE_COOLDOWN_SEC = float(os.environ.get("MLBB_MONTAGE_COOLDOWN_SEC", "14400"))
    HERO_MONTAGE_ENABLED = base.get("MLBB_HERO_SHORTS_MONTAGE", "0") == "1"
    JOB_MIN_GAP_SEC = float(base.get("MLBB_JOB_MIN_GAP_SEC", "45"))
    HERO_MONTAGE_DAILY_MAX = int(base.get("MLBB_HERO_MONTAGE_DAILY_MAX", "40"))
    cores = _cpu_cores()

    log(
        f"mlbb_continuous_worker start cores={cores} vod_disabled={VOD_DISABLED} "
        f"one_heavy={ONE_HEAVY_JOB} calibration_feed={CALIBRATION_FEED} "
        f"target_pending={TARGET_PENDING} feed_cd={FEED_COOLDOWN_SEC}s "
        f"ingest_cd={INGEST_COOLDOWN_SEC}s shorts_days={SHORTS_DAYS} "
        f"hero_montage={HERO_MONTAGE_ENABLED} job_gap={JOB_MIN_GAP_SEC}s "
        f"hero_daily_max={HERO_MONTAGE_DAILY_MAX} loop={LOOP_SEC}s"
    )
    write_state(
        {
            "cores": cores,
            "one_heavy_job": ONE_HEAVY_JOB,
            "pending_shorts": 0,
            "claimed_shorts": 0,
            "feed_ready": False,
            "vod_disabled": VOD_DISABLED,
            "ingest_running": False,
            "vod_running": False,
            "vod_age_sec": 0,
            "feed_running": False,
            "montage_running": False,
            "hero_montage_running": False,
            "cycles": 0,
            "startup": True,
        }
    )
    cycles = 0
    pending = _cached_pending_shorts()

    bootstrap_script = BIN / "mlbb_silver_bootstrap.py"
    if not bootstrap_script.exists():
        bootstrap_script = Path(__file__).resolve().parent / "mlbb_silver_bootstrap.py"
    if bootstrap_script.exists() and pending < 6 and os.environ.get("MLBB_SILVER_BOOTSTRAP", "0") == "1":
        log(f"startup silver bootstrap pending={pending}")
        subprocess.run(
            [
                PY,
                str(bootstrap_script),
                "--youtube-downloads",
                base.get("MLBB_SILVER_YT_DOWNLOADS", "15"),
                "--viral-downloads",
                "0",
                "--telegram",
            ],
            env=base,
            timeout=7200,
            check=False,
        )
        pending = pending_shorts()

    while True:
        cycles += 1
        for job in (ingest, vod, feed, montage, hero_montage):
            job.reap()

        if cycles % 3 == 0:
            keep = ingest.proc.pid if ingest.running() and ingest.proc else None
            dup = prune_duplicate_ingests(keep_pid=keep)
            if dup:
                log(f"pruned_duplicate_ingests={dup}")

        pending = _cached_pending_shorts()
        hungry_threshold = int(base.get("MLBB_INGEST_FORCE_HUNGRY_PENDING", "8"))
        hungry = pending < hungry_threshold
        aggressive = pending > 0 and pending < TARGET_PENDING
        from mlbb_calibration_store import claimed_count, release_stale_claims, last_feed_sent_age_sec

        if cycles % 5 == 0:
            released = release_stale_claims(
                max_age_sec=float(base.get("MLBB_CLAIM_STALE_SEC", "300"))
            )
            if released:
                log(f"released_stale_claims={released}")
                _invalidate_pending_cache()
                pending = _cached_pending_shorts()
            if pending < int(base.get("MLBB_INDEX_DISK_IF_PENDING", "8")):
                from mlbb_calibration_store import index_disk_avail

                indexed = index_disk_avail()
                if indexed:
                    log(f"index_disk_avail={indexed}")
                    _invalidate_pending_cache()
                    pending = _cached_pending_shorts()

        claimed = claimed_count()
        sent_age = last_feed_sent_age_sec()
        if (
            pending == 0
            and sent_age >= float(base.get("MLBB_RECYCLE_SENT_SEC", "1800"))
            and cycles % 10 == 0
        ):
            from mlbb_calibration_store import recycle_unlabeled_sent

            recycled = recycle_unlabeled_sent(limit=int(base.get("MLBB_RECYCLE_LIMIT", "12")))
            if recycled:
                log(f"recycled_unlabeled_sent={recycled}")
                _invalidate_pending_cache()
                pending = _cached_pending_shorts()
                hungry = pending < hungry_threshold

        force_empty_feed = sent_age >= float(base.get("MLBB_FEED_FORCE_EMPTY_SEC", "480")) or (
            feed.last_finish > 0
            and (time.time() - feed.last_finish)
            >= float(base.get("MLBB_FEED_FORCE_EMPTY_SEC", "480"))
        )
        feed_ready = pending > 0 or claimed > 0 or force_empty_feed
        shorts_parallel = VOD_DISABLED and not ONE_HEAVY_JOB

        vod_busy = vod.running() or bool(vod_feed_pids()) or oneoff_running()
        montage_busy = montage.running()
        ingest_block = ONE_HEAVY_JOB and not VOD_DISABLED and (vod_busy or montage_busy)
        heavy_busy = ingest_block or (ONE_HEAVY_JOB and ingest.running() and not VOD_DISABLED)
        hero_busy = hero_montage.running()

        if ingest.running() and ingest.age_sec() > INGEST_STALE_SEC:
            log(f"ingest stale age={int(ingest.age_sec())}s — killing")
            ingest.stop(reason="stale")

        if hero_montage.running() and hero_montage.age_sec() > HERO_MONTAGE_STALE_SEC:
            log(f"hero_montage stale age={int(hero_montage.age_sec())}s — killing")
            hero_montage.stop(reason="stale")

        if feed.running() and feed.age_sec() > float(base.get("MLBB_FEED_STALE_SEC", "900")):
            log(f"feed stale age={int(feed.age_sec())}s — killing")
            feed.stop(reason="stale")

        if not VOD_DISABLED:
            if vod.running() and vod.age_sec() > VOD_STALE_SEC:
                log(f"vod stale age={int(vod.age_sec())}s — killing stuck scan")
                vod.stop(reason="stale")
                for pid in vod_feed_pids():
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass

            if (
                not vod.running()
                and not vod_feed_pids()
                and not oneoff_running()
                and (not ONE_HEAVY_JOB or not montage.running())
            ):
                vod.start()

            if not heavy_busy and not vod.running() and montage.cooldown_ok(MONTAGE_COOLDOWN_SEC):
                montage.start()

        ingest_if_pending = int(base.get("MLBB_INGEST_IF_PENDING", str(TARGET_PENDING)))
        ingest_gap = INGEST_COOLDOWN_SEC if (aggressive or hungry) else JOB_MIN_GAP_SEC
        ingest_mutex = ONE_HEAVY_JOB and not shorts_parallel and (
            feed.running() or calibration_feed_running() or hero_busy
        )
        own_ingest = ingest.proc.pid if ingest.running() and ingest.proc else None
        global_ingest = ingest_running_global()
        if (
            pending < ingest_if_pending
            and ingest.gap_ok(ingest_gap)
            and not ingest_block
            and not ingest_mutex
            and (not global_ingest or own_ingest is not None)
        ):
            ingest.cmd = ingest_cmd(base, aggressive=aggressive, hungry=hungry)
            ingest.env = ingest_env(base, aggressive=aggressive, hungry=hungry)
            ingest.start()

        hero_min_pending = int(base.get("MLBB_HERO_MONTAGE_MIN_PENDING", str(TARGET_PENDING + 5)))
        if (
            HERO_MONTAGE_ENABLED
            and not VOD_DISABLED
            and pending >= hero_min_pending
            and not hero_busy
            and not feed.running()
            and not ingest.running()
            and hero_montage.gap_ok(JOB_MIN_GAP_SEC)
            and hero_montages_today() < HERO_MONTAGE_DAILY_MAX
            and (not ONE_HEAVY_JOB or not vod_busy)
        ):
            hero_montage.start()

        if pending > 0:
            feed_cd = float(base.get("MLBB_FEED_COOLDOWN_PENDING_SEC", str(FEED_COOLDOWN_SEC)))
        else:
            feed_cd = float(base.get("MLBB_FEED_COOLDOWN_EMPTY_SEC", str(FEED_COOLDOWN_SEC)))
        feed_mutex = ONE_HEAVY_JOB and not shorts_parallel and ingest.running()
        stray_feed = calibration_feed_running() and not feed.running()
        if (
            CALIBRATION_FEED
            and feed_ready
            and feed.gap_ok(feed_cd)
            and (VOD_DISABLED or not vod.running())
            and not stray_feed
            and not feed_mutex
        ):
            feed.start()

        if VOD_DISABLED and vod_feed_pids():
            for pid in vod_feed_pids():
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass

        for job in (ingest, vod, feed, montage, hero_montage):
            rc = job.reap()
            if rc is not None and job.name == "feed" and rc != 0:
                from mlbb_calibration_store import release_stale_claims

                released = release_stale_claims(max_age_sec=60)
                if released:
                    log(f"feed_failed rc={rc} released_stale_claims={released}")
                _invalidate_pending_cache()
            if rc is not None and job.name in ("ingest", "hero_montage", "montage"):
                _invalidate_pending_cache()
                cleanup_script = BIN / "mlbb_runtime_cleanup.py"
                if not cleanup_script.exists():
                    cleanup_script = Path(__file__).resolve().parent / "mlbb_runtime_cleanup.py"
                if cleanup_script.exists():
                    subprocess.run([PY, str(cleanup_script)], env=base, timeout=30, check=False)

        if cycles % 180 == 0 and not hero_busy and not ingest.running():
            train_script = BIN / "highlight_train.py"
            if not train_script.exists():
                train_script = Path(__file__).resolve().parent / "highlight_train.py"
            if train_script.exists() and base.get("MLBB_AUTO_TRAIN", "1") == "1":
                subprocess.run(
                    [PY, str(train_script), "--profile", "mobile_legends"],
                    env={**base, "MLBB_USE_CLASSIFIER": "1"},
                    timeout=600,
                    check=False,
                )

        if cycles % 90 == 0 and not heavy_busy:
            sync_script = BIN / "mlbb_viral_threshold_sync.py"
            if not sync_script.exists():
                sync_script = Path(__file__).resolve().parent / "mlbb_viral_threshold_sync.py"
            subprocess.run([PY, str(sync_script)], env=base, timeout=120, check=False)

        if cycles % 360 == 0:
            report_script = BIN / "mlbb_daily_report.py"
            if not report_script.exists():
                report_script = Path(__file__).resolve().parent / "mlbb_daily_report.py"
            subprocess.run([PY, str(report_script), "--telegram"], env=base, timeout=60, check=False)

        if cycles % 45 == 0 and base.get("MLBB_LEARNING_FIRST", "0") == "1":
            eval_script = BIN / "eval_learning_first_gate.py"
            if not eval_script.exists():
                eval_script = Path(__file__).resolve().parent / "eval_learning_first_gate.py"
            subprocess.run(
                [PY, str(eval_script)],
                env=base,
                timeout=3600,
                check=False,
            )

        if cycles % 30 == 0 and aggressive and not ingest.running():
            backfill_script = BIN / "mlbb_calibration_store.py"
            subprocess.run(
                [
                    PY,
                    "-c",
                    "import sys; sys.path.insert(0,'/usr/local/bin'); "
                    "from mlbb_calibration_store import backfill_gameplay_flags; "
                    f"print('backfill', backfill_gameplay_flags(limit={int(base.get('MLBB_WORKER_BACKFILL_LIMIT', '8'))}))",
                ],
                env=base,
                timeout=120,
                check=False,
            )
            _invalidate_pending_cache()

        if cycles % 5 == 0:
            write_state(
                {
                    "cores": cores,
                    "one_heavy_job": ONE_HEAVY_JOB,
                    "pending_shorts": pending,
                    "claimed_shorts": claimed,
                    "feed_ready": feed_ready,
                    "vod_disabled": VOD_DISABLED,
                    "ingest_running": ingest.running(),
                    "vod_running": vod.running(),
                    "vod_age_sec": int(vod.age_sec()),
                    "feed_running": feed.running(),
                    "montage_running": montage.running(),
                    "hero_montage_running": hero_montage.running(),
                    "cycles": cycles,
                }
            )

        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
