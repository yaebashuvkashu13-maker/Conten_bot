#!/usr/bin/env python3
"""Evening status report to owner Telegram."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ENV_FILE = Path("/root/.video_bot.env")
HERO_ROOT = Path("/root/hero_datasets")
VIDEO_DIR = Path("/root/videos")
BOT_LOG = Path("/root/telegram_upload_bot.log")
EDITOR_LOG = Path("/root/smart_video_editor.log")
STATE_DIR = Path("/root/data/mlbb/daily_ops")


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


def send_text(token: str, chat_id: str, text: str) -> bool:
    clean = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    result = subprocess.run(
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
        return bool(json.loads(result.stdout).get("ok"))
    except json.JSONDecodeError:
        return False


def count_mp4(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for _ in root.rglob("*.mp4"))


def today_videos() -> list[str]:
    if not VIDEO_DIR.exists():
        return []
    today = time.strftime("%Y%m%d")
    rows = []
    for path in sorted(VIDEO_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        if today in path.name or path.stat().st_mtime > time.time() - 86400:
            rows.append(f"• {path.name} ({path.stat().st_size // (1024*1024)} MB)")
        if len(rows) >= 8:
            break
    return rows


def log_errors_today(path: Path, needle: str, limit: int = 5) -> int:
    if not path.exists():
        return 0
    today = time.strftime("%Y-%m-%d")
    count = 0
    samples: list[str] = []
    for line in path.read_text(errors="replace").splitlines()[-4000:]:
        if today not in line:
            continue
        if needle in line:
            count += 1
            if len(samples) < limit:
                samples.append(line.strip()[-120:])
    return count


def instagram_digest_status() -> str:
    log_path = Path("/root/data/mlbb/instagram_digest.log")
    if not log_path.exists():
        return "Instagram: лог не найден"
    today = time.strftime("%Y-%m-%d")
    for line in reversed(log_path.read_text(errors="replace").splitlines()):
        if "done sent=" not in line or today not in line:
            continue
        tail = line.split("done sent=")[-1][:40]
        return f"Instagram сегодня: sent={tail}"
    return "Instagram: сегодня дайджест не завершался (cron 19:00 МСК)"


def proxy_ok() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["python3", "/usr/local/bin/proxy_health_check.py"],
            capture_output=True,
            text=True,
            timeout=40,
        )
        line = (result.stdout or "").strip().splitlines()[-1] if result.stdout else ""
        return result.returncode == 0, line or "unknown"
    except Exception as exc:
        return False, str(exc)


def build_report() -> str:
    day = time.strftime("%Y-%m-%d")
    heroes = count_mp4(HERO_ROOT)
    tiktok_mlbb = count_mp4(Path("/root/datasets/tiktok/mlbb"))
    pubg_tiktok = count_mp4(Path("/root/datasets/tiktok/pubg"))
    vids = today_videos()
    ok_proxy, proxy_line = proxy_ok()
    ig_line = instagram_digest_status()
    bot_err = log_errors_today(BOT_LOG, "ERROR")
    editor_err = log_errors_today(EDITOR_LOG, "ERROR")
    completed = log_errors_today(EDITOR_LOG, "completed successfully")

    done_lines = [
        "✅ Telegram: API без мёртвого прокси (бот + отправка видео)",
        "✅ PUBG коллеге: досланы 3 старых ролика + сообщение о восстановлении",
        f"✅ hero_datasets: {heroes} клипов (работа без TikTok-прокси)",
    ]
    if vids:
        done_lines.append("✅ Ролики за сутки:")
        done_lines.extend(vids[:6])

    todo_lines = []
    if not ok_proxy:
        todo_lines.append("⏸ TikTok-скачивание: прокси мёртв — нужен новый CyberYozh (или пауза)")
    todo_lines.append("🎯 MLBB эталон: ждём твой фидбек по Ling (звук / обучение / куски)")
    todo_lines.append("🎯 PUBG: проверить первую новую загрузку коллеги после фикса")

    help_lines = []
    if not ok_proxy:
        help_lines.append("• Креды нового прокси в .video_bot.env — запустим burst gameplay-only")
    help_lines.append("• 2–3 конкретных замечания по последнему эталону — правим фильтры точечно")

    return f"""🌙 Отчёт за {day}

Сделано
{chr(10).join(done_lines)}

Не сделано / в очереди
{chr(10).join(todo_lines)}

Метрики
• smart_edit ok сегодня: ~{completed} (по логу)
• ошибки бота сегодня: {bot_err}
• ошибки редактора: {editor_err}
• tiktok mlbb mp4: {tiktok_mlbb} | pubg tiktok: {pubg_tiktok}
• прокси: {proxy_line}
• {ig_line}

Нужна помощь
{chr(10).join(help_lines)}

Завтра утром — новый план в этот чат."""


def main() -> int:
    env = load_env(ENV_FILE)
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1
    text = build_report()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"evening_{time.strftime('%Y%m%d')}.txt").write_text(text, encoding="utf-8")
    ok = send_text(token, chat_id, text)
    print(f"evening_report sent={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
