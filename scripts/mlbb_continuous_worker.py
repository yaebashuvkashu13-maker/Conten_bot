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
LOOP_SEC = float(os.environ.get("MLBB_CONTINUOUS_LOOP_SEC", "4"))


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
            "MLBB_CALIBRATION_BATCH": env.get("MLBB_CALIBRATION_BATCH", "12"),
            "MLBB_SHORTS_CALIBRATION_BURST": env.get("MLBB_SHORTS_CALIBRATION_BURST", "1"),
            "MLBB_CALIBRATION_LENIENT": env.get("MLBB_CALIBRATION_LENIENT", "1"),
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
        return (time.time() - self.last_start) >= sec or not self.running()


def pending_shorts() -> int:
    from mlbb_calibration_store import pending_candidates, rebuild_index_from_disk

    try:
        rebuild_index_from_disk()
    except Exception as exc:
        log(f"rebuild_index_from_disk skipped: {exc}")
    try:
        return len(pending_candidates(limit=9999))
    except Exception as exc:
        log(f"pending_candidates error: {exc}")
        return 0


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
    max_dl = env.get("MLBB_INGEST_MAX_DOWNLOADS", "40" if burst else "15")
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
    ingest_cooldown = float(base.get("MLBB_INGEST_COOLDOWN_SEC", "8"))
    feed_cooldown = float(base.get("MLBB_FEED_COOLDOWN_SEC", "180"))
    if base.get("MLBB_SEND_ENABLED", "1") != "1":
        log("MLBB_SEND_ENABLED=0 — worker idle (no Telegram sends)")
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

            if aggressive and ingest.cooldown_ok(ingest_cooldown):
                ingest.cmd = ingest_cmd(base, aggressive=aggressive)
                ingest.env = ingest_env(base, aggressive=aggressive)
                ingest.start()

            if not vod.running():
                vod.start()

            if pending > 0 and not feed.running() and feed.cooldown_ok(feed_cooldown):
                feed.start()

            if montage_enabled and montage.cooldown_ok(MONTAGE_COOLDOWN_SEC):
                montage.start()

            for job in (ingest, vod, feed, montage):
                job.reap()

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
                write_state(
                    {
                        "pending_shorts": pending,
                        "ingest_running": ingest.running(),
                        "vod_running": vod.running(),
                        "feed_running": feed.running(),
                        "cycles": cycles,
                        "worker_pid": os.getpid(),
                    }
                )
        except Exception as exc:
            log(f"loop error: {type(exc).__name__}: {exc}")

        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
