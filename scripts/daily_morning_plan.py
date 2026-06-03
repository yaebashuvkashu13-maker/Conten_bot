#!/usr/bin/env python3
"""Send owner a concise morning ops plan via Telegram."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ENV_FILE = Path("/root/.video_bot.env")
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
        payload = json.loads(result.stdout)
        return bool(payload.get("ok"))
    except json.JSONDecodeError:
        return False


def build_plan() -> str:
    day = time.strftime("%Y-%m-%d")
    return f"""🌅 План на {day}

Цели дня
1) MLBB — стабильная эталонная нарезка (1 герой, бой, звук игры, 33–57 с).
2) PUBG — коллега снова получает ролики в чат без сбоев.
3) Не жечь ресурсы впустую (без прокси — только локальная работа).

Задачи
• Принять фидбек по эталону Ling и сделать 1 правку/новый образец при необходимости.
• Проверить 1–2 новых загрузки коллеги PUBG (профиль pubg, доставка видео).
• Держать один процесс бота; Telegram без HTTP_PROXY.
• hero_datasets: нарезки и сортировка по героям (без TikTok, пока нет прокси).
• Вечером — отчёт: что сделано / что нет / нужна ли помощь.

Нужно от тебя (если есть)
• Новый прокси — только когда готовы к burst TikTok (gameplay-only).
• 2–3 пункта по эталону Ling (звук / обучение / кусок N).

Итог дня: хотя бы 1 эталон MLBB + подтверждённый цикл PUBG для коллеги."""


def main() -> int:
    env = load_env(ENV_FILE)
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1
    text = build_plan()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"morning_{time.strftime('%Y%m%d')}.txt").write_text(text, encoding="utf-8")
    ok = send_text(token, chat_id, text)
    print(f"morning_plan sent={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
