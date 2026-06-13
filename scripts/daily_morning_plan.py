#!/usr/bin/env python3
"""Send owner a concise morning ops plan via Telegram."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

ENV_FILE = Path("/root/.video_bot.env")
STATE_DIR = Path("/root/data/mlbb/daily_ops")
OVERNIGHT_ROOT = Path("/root/data/mlbb/overnight_msk")
OVERNIGHT_REPORT = OVERNIGHT_ROOT / "last_report.json"
OVERNIGHT_STATE = OVERNIGHT_ROOT / "state.json"
GAMES_CONFIG = Path(
    os.environ.get("OVERNIGHT_GAMES_CONFIG", "/root/content_bot_ml/config/overnight_games.yaml")
)

GAME_LABELS = {
    "mlbb": "MLBB",
    "pubg": "PUBG Metro",
    "genshin": "Genshin",
    "standoff": "Standoff 2",
    "wot": "WoT PC",
}


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
        payload = json.loads(result.stdout)
        return bool(payload.get("ok"))
    except json.JSONDecodeError:
        return False


def load_game_ids() -> list[str]:
    if GAMES_CONFIG.exists():
        try:
            cfg = yaml.safe_load(GAMES_CONFIG.read_text(encoding="utf-8")) or {}
            games = cfg.get("games") or []
            return [g["id"] for g in games if g.get("id")]
        except (yaml.YAMLError, KeyError, TypeError):
            pass
    return list(GAME_LABELS.keys())


def overnight_game_lines() -> list[str]:
    game_ids = load_game_ids()
    status_by_game: dict[str, dict] = {}
    if OVERNIGHT_STATE.exists():
        try:
            state = json.loads(OVERNIGHT_STATE.read_text(encoding="utf-8"))
            status_by_game = state.get("game_status") or {}
        except json.JSONDecodeError:
            pass

    lines: list[str] = []
    for gid in game_ids:
        label = GAME_LABELS.get(gid, gid)
        row = status_by_game.get(gid) or {}
        st = row.get("status") or ""
        ok_n = int(row.get("montages_ok") or 0)
        skipped = row.get("skipped")
        if st == "ok" and ok_n > 0:
            lines.append(f"  ✅ {label}")
        elif skipped:
            lines.append(f"  ⏳ {label} — {skipped}")
        elif st in ("pending", "download_failed", "montage_failed", "no_candidate"):
            lines.append(f"  ⏳ {label} — {st}")
        else:
            lines.append(f"  ⏳ {label} — ждём")
    return lines


def overnight_summary() -> str:
    if not OVERNIGHT_REPORT.exists() and not OVERNIGHT_STATE.exists():
        return "• Ночной батч: отчёта нет (cron 18:00 МСК или catch-up вручную)."

    if OVERNIGHT_REPORT.exists():
        try:
            data = json.loads(OVERNIGHT_REPORT.read_text(encoding="utf-8"))
            results = data.get("results") or []
            if results:
                done = sum(int(r.get("montages_ok") or 0) for r in results)
                total = len(load_game_ids())
                started = data.get("started", "?")
                return (
                    f"• Ночной батч (старт {started}): {done}/{total} нарезок.\n"
                    + "\n".join(overnight_game_lines())
                )
        except json.JSONDecodeError:
            pass

    lines = overnight_game_lines()
    if lines:
        return "• Ночной батч (прогресс):\n" + "\n".join(lines)
    return "• Ночной батч: данных мало — смотрим лог batch.log."


def batch_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "overnight_youtube_batch.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def build_plan() -> str:
    day = time.strftime("%Y-%m-%d")
    game_list = ", ".join(GAME_LABELS.get(g, g) for g in load_game_ids())
    overnight = overnight_summary()
    running = "🔄 Сейчас на сервере идёт нарезка (catch-up/ночной батч)." if batch_running() else ""

    return f"""🌅 План на {day}

Главная цель дня
🎯 5 нарезок по играм (по 1 на игру): {game_list}.
Ролики приходят в Telegram по мере готовности — не ждём все пять разом.

Задачи
• Догнать/завершить ночной батч: MLBB → PUBG Metro → Genshin → Standoff → WoT.
• Если игра упала — retry и fallback URL, остальные игры не останавливаем.
• Один активный smart_video_editor; YouTube без мёртвого HTTP_PROXY.
• Вечером — отчёт: сколько из 5 готово / что осталось.

{overnight}
{running}

Итог дня: 5/5 нарезок в чат (или явный статус по каждой игре, если что-то не собралось)."""


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
