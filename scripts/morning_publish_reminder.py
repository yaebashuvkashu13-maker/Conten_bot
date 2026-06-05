#!/usr/bin/env python3
"""Morning publish status (5-game batch). Plan is sent by daily_morning_plan.py only."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

ENV_FILE = Path("/root/.video_bot.env")
MANIFEST = Path("/root/data/mlbb/publish/latest_montage.json")
OVERNIGHT_REPORT = Path("/root/data/mlbb/overnight_msk/last_report.json")
OVERNIGHT_STATE = Path("/root/data/mlbb/overnight_msk/state.json")
PENDING_ROOT = Path("/root/telegram_uploads/pending")

GAME_LABELS = {
    "mlbb": "MLBB",
    "pubg": "PUBG Metro",
    "genshin": "Genshin",
    "standoff": "Standoff 2",
    "wot": "WoT PC",
}


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def send_text(token: str, chat_id: str, text: str) -> bool:
    clean = {k: v for k, v in __import__("os").environ.items() if "proxy" not in k.lower()}
    proc = subprocess.run(
        [
            "curl",
            "--noproxy",
            "*",
            "-sS",
            "-m",
            "60",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"text={text[:3900]}",
            f"https://api.telegram.org/bot{token}/sendMessage",
        ],
        capture_output=True,
        text=True,
        env=clean,
    )
    try:
        return bool(json.loads(proc.stdout or "{}").get("ok"))
    except json.JSONDecodeError:
        return False


def pending_youtube_count(chat_id: str) -> int:
    pending = PENDING_ROOT / chat_id
    if not pending.exists():
        return 0
    return len(list(pending.glob("*_youtube_*.mp4")))


def overnight_status_lines() -> list[str]:
    lines: list[str] = []
    status_by_game: dict[str, dict] = {}
    if OVERNIGHT_STATE.exists():
        try:
            state = json.loads(OVERNIGHT_STATE.read_text(encoding="utf-8"))
            status_by_game = state.get("game_status") or {}
        except json.JSONDecodeError:
            pass

    for gid, label in GAME_LABELS.items():
        row = status_by_game.get(gid) or {}
        if row.get("status") == "ok" and int(row.get("montages_ok") or 0) > 0:
            lines.append(f"✅ {label}")
        elif row.get("skipped") or row.get("status"):
            detail = row.get("skipped") or row.get("status")
            lines.append(f"⏳ {label}: {detail}")
        else:
            lines.append(f"⏳ {label}")

    if OVERNIGHT_REPORT.exists():
        try:
            rep = json.loads(OVERNIGHT_REPORT.read_text(encoding="utf-8"))
            results = rep.get("results") or []
            done = sum(int(r.get("montages_ok") or 0) for r in results)
            if done:
                lines.insert(0, f"Батч: {done}/5 нарезок")
        except json.JSONDecodeError:
            pass
    return lines


def main() -> int:
    env = load_env()
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing")
        return 1

    lines = [f"📤 Статус нарезок ({time.strftime('%d.%m')})"]
    batch_lines = overnight_status_lines()
    if batch_lines:
        lines.append("Ночной батч (5 игр):\n" + "\n".join(batch_lines))

    if MANIFEST.exists():
        try:
            data = json.loads(MANIFEST.read_text(encoding="utf-8"))
            lines.append(
                f"Последний манифест: {data.get('name', '?')} ({data.get('size_mb', '?')} МБ)\n"
                f"{data.get('path', '')}"
            )
        except json.JSONDecodeError:
            lines.append("Манифест битый — см. /root/videos/")
    else:
        latest = sorted(Path("/root/videos").glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if latest:
            lines.append(f"Последняя нарезка на диске: {latest[0].name}")

    yt_n = pending_youtube_count(chat_id)
    if yt_n:
        lines.append(f"YouTube в очереди бота: {yt_n} шт. → /make")

    ok = send_text(token, chat_id, "\n\n".join(lines))
    print(f"publish_reminder sent={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
