#!/usr/bin/env python3
"""Hourly Telegram progress report for MLBB video pipeline."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

ENV_FILE = Path("/root/.video_bot.env")
STATE_PATH = Path("/root/data/mlbb/download_state.json")
HISTORY_PATH = Path("/root/.smart_edit_segment_history.json")
VIDEOS_DIR = Path("/root/videos")
PREVIEW_DIR = Path("/root/hourly_previews")
DATASET_DIR = Path("/root/datasets/tiktok/mlbb")
REPORT_STATE = Path("/root/data/mlbb/last_progress_report.json")


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def tg_send_message(token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:3900]}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode())
    if not payload.get("ok"):
        raise RuntimeError(payload)


def tg_send_video(token: str, chat_id: str, video: Path, caption: str) -> None:
    subprocess.run(
        [
            "curl",
            "-sS",
            "--max-time",
            "600",
            "-X",
            "POST",
            f"https://api.telegram.org/bot{token}/sendVideo",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"caption={caption[:1024]}",
            "-F",
            "supports_streaming=true",
            "-F",
            f"video=@{video}",
        ],
        check=True,
        timeout=620,
    )


def count_mp4(folder: Path) -> int:
    if not folder.exists():
        return 0
    return sum(1 for _ in folder.rglob("*.mp4"))


def latest_video(folder: Path, since_ts: float) -> Path | None:
    if not folder.exists():
        return None
    candidates = [p for p in folder.glob("*.mp4") if p.stat().st_mtime >= since_ts]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def disk_free_gb() -> float:
    out = subprocess.check_output(["df", "-BG", "/"], text=True).splitlines()[-1].split()
    avail = out[3].rstrip("G")
    return float(avail)


def build_report() -> str:
    download = {}
    if STATE_PATH.exists():
        download = json.loads(STATE_PATH.read_text())
    hist_segments = 0
    if HISTORY_PATH.exists():
        try:
            hist_segments = len(json.loads(HISTORY_PATH.read_text()).get("segment_keys", []))
        except Exception:
            pass

    stats = download.get("last_stats") or {}
    lines = [
        "📊 MLBB — отчёт за час",
        "",
        "🎬 Smart Edit (правила):",
        "• 3–4 сцены, 33–57 сек",
        "• без повторов сцен",
        "• только геймплей",
        "",
        f"⬇️ TikTok за последний цикл: +{stats.get('gameplay_kept', 0)} геймплей "
        f"(скачано {stats.get('downloaded', 0)}, отбраковано {stats.get('rejected', 0)})",
        f"📁 Датасет на сервере: {count_mp4(DATASET_DIR)} mp4",
        f"🧠 Hayabusa датасет: {count_mp4(Path('/root/hero_datasets/hayabusa'))} mp4",
        f"🔁 Уже использовано сцен: {hist_segments}",
        f"💾 Свободно на диске: {disk_free_gb():.1f} GB",
        "",
        "🎯 Сейчас учим: Hayabusa → потом 10 героев",
    ]
    last_run = download.get("last_run")
    if last_run:
        lines.insert(2, f"🕒 Цикл данных: {last_run}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, default=ENV_FILE)
    parser.add_argument("--attach-latest-video", action="store_true")
    args = parser.parse_args()

    env = load_env(args.env)
    token = env.get("TG_BOT_TOKEN")
    chat_id = env.get("TG_CHAT_ID")
    if not token or not chat_id:
        raise SystemExit("TG_BOT_TOKEN / TG_CHAT_ID missing")

    now = time.time()
    since = now - 3700
    report = build_report()
    tg_send_message(token, chat_id, report)

    if args.attach_latest_video:
        clip = latest_video(VIDEOS_DIR, since) or latest_video(PREVIEW_DIR, since)
        if clip:
            tg_send_video(token, chat_id, clip, f"Новый клип: {clip.name}")

    REPORT_STATE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_STATE.write_text(json.dumps({"sent_at": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
