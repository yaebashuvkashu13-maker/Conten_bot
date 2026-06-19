#!/usr/bin/env python3
"""MLBB 24/7 worker — YouTube Shorts ingest + calibration feed (optional VOD).

Default with MLBB_VOD_DISABLED=1: fresh Shorts only (~15/hour to Telegram).
VOD/montage stay off unless explicitly re-enabled on the server.
"""

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
ONEOFF_LOCK = Path("/tmp/mlbb_vod_oneoff.lock")


def oneoff_running() -> bool:
    return ONEOFF_LOCK.exists()


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
    """Drop CLIP-heavy children left from crashed/restarted workers."""
    patterns = ["mlbb_youtube_shorts_ingest.py"]
    if VOD_DISABLED:
        patterns.extend(
            (
                "mlbb_vod_segment_feed.py",
                "mlbb_vod_montage_feed.py",
                "mlbb_vod_oneoff.py",
            )
        )
    else:
        patterns.extend(("mlbb_vod_segment_feed.py", "mlbb_vod_montage_feed.py"))
    for pat in patterns:
        subprocess.run(["pkill", "-f", pat], check=False)
    if VOD_DISABLED:
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
        env.setdefault(key.strip(), val.strip().strip('"').strip("'"))
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
            "MLBB_SHORTS_RESEND_IF_UNLABELED": env.get("MLBB_SHORTS_RESEND_IF_UNLABELED", "1"),
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
        return (time.time() - self.last_start) >= sec


def pending_shorts() -> int:
    from mlbb_calibration_store import pending_candidates, rebuild_index_from_disk

    rebuild_index_from_disk()
    return len(pending_candidates(limit=9999))


def ingest_cmd(env: dict[str, str], *, aggressive: bool) -> list[str]:
    script = BIN / "mlbb_youtube_shorts_ingest.py"
    if not script.exists():
        script = Path(__file__).resolve().parent / "mlbb_youtube_shorts_ingest.py"
    max_dl = "20" if aggressive else "6"
    max_q = "30" if aggressive else "12"
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


def ingest_env(base: dict[str, str], *, aggressive: bool) -> dict[str, str]:
    env = dict(base)
    env.update(
        {
            "MLBB_CALIBRATION_LENIENT": "1",
            "MLBB_CALIBRATION_FAST_INGEST": env.get("MLBB_CALIBRATION_FAST_INGEST", "1"),
            "MLBB_SHORTS_DAYS": env.get("MLBB_SHORTS_DAYS", "365"),
            "MLBB_SHORTS_MIN_YEAR": env.get("MLBB_SHORTS_MIN_YEAR", "2025"),
            "MLBB_SHORTS_YEAR_ONLY": env.get("MLBB_SHORTS_YEAR_ONLY", "1"),
            "MLBB_SHORTS_RESEND_IF_UNLABELED": env.get("MLBB_SHORTS_RESEND_IF_UNLABELED", "1"),
            "MLBB_INGEST_SKIP_IF_PENDING": "0" if aggressive else env.get("MLBB_INGEST_SKIP_IF_PENDING", "6"),
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
    global FEED_COOLDOWN_SEC, INGEST_COOLDOWN_SEC, TARGET_PENDING, CALIBRATION_FEED, VOD_DISABLED, SHORTS_DAYS
    FEED_COOLDOWN_SEC = float(base.get("MLBB_FEED_COOLDOWN_SEC", str(FEED_COOLDOWN_SEC)))
    INGEST_COOLDOWN_SEC = float(base.get("MLBB_INGEST_COOLDOWN_SEC", str(INGEST_COOLDOWN_SEC)))
    TARGET_PENDING = int(base.get("MLBB_TARGET_PENDING", str(TARGET_PENDING)))
    CALIBRATION_FEED = base.get("MLBB_CALIBRATION_FEED_ENABLED", "1" if VOD_DISABLED else "0") == "1"
    VOD_DISABLED = base.get("MLBB_VOD_DISABLED", "1") == "1"
    global SHORTS_DAYS
    SHORTS_DAYS = int(base.get("MLBB_SHORTS_DAYS", "365"))
    kill_orphan_heavy_procs()
    time.sleep(2)
    ingest = Proc("ingest", [], ingest_env(base, aggressive=True))
    vod = Proc("vod", vod_cmd(base), vod_env(base))
    feed = Proc("feed", feed_cmd(base), base)
    montage = Proc("montage", montage_cmd(base), base)
    MONTAGE_COOLDOWN_SEC = float(os.environ.get("MLBB_MONTAGE_COOLDOWN_SEC", "14400"))
    cores = _cpu_cores()

    log(
        f"mlbb_continuous_worker start cores={cores} vod_disabled={VOD_DISABLED} "
        f"one_heavy={ONE_HEAVY_JOB} calibration_feed={CALIBRATION_FEED} "
        f"target_pending={TARGET_PENDING} feed_cd={FEED_COOLDOWN_SEC}s "
        f"ingest_cd={INGEST_COOLDOWN_SEC}s shorts_days={SHORTS_DAYS} loop={LOOP_SEC}s"
    )
    cycles = 0
    pending = pending_shorts()

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
        pending = pending_shorts()
        aggressive = pending < TARGET_PENDING

        vod_busy = vod.running() or bool(vod_feed_pids()) or oneoff_running()
        montage_busy = montage.running()
        ingest_block = ONE_HEAVY_JOB and not VOD_DISABLED and (vod_busy or montage_busy)
        heavy_busy = ingest_block or (ONE_HEAVY_JOB and ingest.running() and not VOD_DISABLED)

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

        if aggressive and ingest.cooldown_ok(INGEST_COOLDOWN_SEC) and not ingest_block:
            ingest.cmd = ingest_cmd(base, aggressive=aggressive)
            ingest.env = ingest_env(base, aggressive=aggressive)
            ingest.start()

        if (
            CALIBRATION_FEED
            and feed.cooldown_ok(FEED_COOLDOWN_SEC)
            and (VOD_DISABLED or not vod.running())
        ):
            feed.start()

        if VOD_DISABLED and vod_feed_pids():
            for pid in vod_feed_pids():
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass

        for job in (ingest, vod, feed, montage):
            job.reap()

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

        if cycles % 15 == 0:
            write_state(
                {
                    "cores": cores,
                    "one_heavy_job": ONE_HEAVY_JOB,
                    "pending_shorts": pending,
                    "vod_disabled": VOD_DISABLED,
                    "ingest_running": ingest.running(),
                    "vod_running": vod.running(),
                    "vod_age_sec": int(vod.age_sec()),
                    "feed_running": feed.running(),
                    "montage_running": montage.running(),
                    "cycles": cycles,
                }
            )

        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
