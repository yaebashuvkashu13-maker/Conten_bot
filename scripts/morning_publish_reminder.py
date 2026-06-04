#!/usr/bin/env python3
"""Morning Telegram: plan + ready montage + YouTube queue status."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

ENV_FILE = Path("/root/.video_bot.env")
MANIFEST = Path("/root/data/mlbb/publish/latest_montage.json")
REPORT = Path("/root/data/mlbb/youtube_nightly/last_report.json")
PENDING_ROOT = Path("/root/telegram_uploads/pending")


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


def main() -> int:
    env = load_env()
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing")
        return 1

    subprocess.run(
        ["python3", "/usr/local/bin/daily_morning_plan.py"],
        check=False,
    )

    lines = [f"📤 Публикация ({time.strftime('%d.%m')})"]
    if MANIFEST.exists():
        try:
            data = json.loads(MANIFEST.read_text(encoding="utf-8"))
            lines.append(
                f"Готовый ролик: {data.get('name', '?')} ({data.get('size_mb', '?')} МБ)\n"
                f"Путь на VPS: {data.get('path', '')}\n"
                f"TikTok / YouTube Shorts / Reels — загрузите вручную или позже авто."
            )
        except json.JSONDecodeError:
            lines.append("Манифест нарезки битый — см. /root/videos/")
    else:
        latest = sorted(Path("/root/videos").glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if latest:
            lines.append(f"Последняя нарезка: {latest[0].name} (без manifest)")
        else:
            lines.append("Новой нарезки в /root/videos нет.")

    yt_n = pending_youtube_count(chat_id)
    if yt_n:
        lines.append(f"YouTube в очереди бота: {yt_n} шт. → /make")

    if REPORT.exists():
        try:
            rep = json.loads(REPORT.read_text(encoding="utf-8"))
            if rep.get("ok"):
                lines.append(f"Ночной YouTube: ✅ {rep.get('title', '')[:60]}")
            elif rep.get("phase") != "discover":
                lines.append(f"Ночной YouTube: ⚠️ {rep.get('error', rep.get('message', '?'))}")
        except json.JSONDecodeError:
            pass

    ok = send_text(token, chat_id, "\n\n".join(lines))
    print(f"publish_reminder sent={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
