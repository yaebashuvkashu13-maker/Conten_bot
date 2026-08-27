#!/usr/bin/env python3
"""Owner Telegram controls: process status and exhausted-inbox reset (text commands)."""

from __future__ import annotations

import subprocess
from typing import Iterable

from reset_vod_inbox_exhausted import reset_game
from vod_game_registry import VOD_GAMES, load_state, save_state
from vod_pipeline_health import health_row

# Legacy callback ids (old inline messages); no keyboards are attached anymore.
CALLBACK_RESET = "ops_reset"
CALLBACK_PROCESS = "ops_process"

PROCESS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("telegram_bot", "telegram_upload_bot.py"),
    ("vod_supervisor", "mlbb_vod_segment_feed.sh"),
    ("daily_cycle", "daily_cycle_runner.py"),
    ("mlbb_feed", "mlbb_vod_segment_feed.py"),
    ("shooter_feed", "shooter_vod_segment_feed.py"),
)

PROCESS_LABELS = {
    "telegram_bot": "бот Telegram",
    "vod_supervisor": "супервизор VOD",
    "daily_cycle": "дневной цикл",
    "mlbb_feed": "скан MLBB",
    "shooter_feed": "скан PUBG/Standoff/др.",
}

GAME_ALIASES = {
    "all": "all",
    "все": "all",
    "mlbb": "mlbb",
    "млбб": "mlbb",
    "pubg": "pubg",
    "пабг": "pubg",
    "standoff": "standoff",
    "стендоф": "standoff",
    "стендофф": "standoff",
    "genshin": "genshin",
    "геншин": "genshin",
    "wot": "wot",
    "вот": "wot",
}

DEFAULT_VOD_SEARCH_LIMIT = 50
DEFAULT_VOD_SEARCH_BATCH = 6


def _norm_text(text: str) -> str:
    return " ".join((text or "").strip().split()).lower()


def is_process_command(text: str) -> bool:
    raw = (text or "").strip()
    token = raw.split()[0].split("@")[0].lower() if raw else ""
    if token in ("/process", "/процесс", "/proc"):
        return True
    return _norm_text(raw) in {
        "процесс",
        "process",
        "статус пайплайна",
    }


def is_reset_command(text: str) -> bool:
    raw = (text or "").strip()
    token = raw.split()[0].split("@")[0].lower() if raw else ""
    if token in ("/reset", "/сброс"):
        return True
    return _norm_text(raw) in {
        "сброс",
        "reset",
        "сброс процесса",
        "reset process",
    }


def parse_reset_game(text: str) -> str:
    parts = (text or "").strip().split()
    if len(parts) < 2:
        return "all"
    alias = GAME_ALIASES.get(parts[1].strip().lower(), "")
    if alias:
        return alias
    raise ValueError(f"неизвестная игра {parts[1]!r}: mlbb, pubg, standoff, genshin, wot или all")


def running_processes() -> dict[str, bool]:
    out: dict[str, bool] = {}
    for name, pattern in PROCESS_PATTERNS:
        try:
            proc = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            out[name] = proc.returncode == 0 and bool((proc.stdout or "").strip())
        except (OSError, subprocess.TimeoutExpired):
            out[name] = False
    return out


def _alive(flag: bool) -> str:
    return "работает" if flag else "нет"


def format_process_report(
    *,
    running: dict[str, bool] | None = None,
    rows: Iterable[dict] | None = None,
) -> str:
    running = running if running is not None else running_processes()
    rows = list(rows) if rows is not None else [health_row(g) for g in VOD_GAMES]

    lines = ["📊 Процесс пайплайна"]
    proc_bits = [
        f"{PROCESS_LABELS[name]}: {_alive(running.get(name, False))}"
        for name, _pat in PROCESS_PATTERNS
    ]
    lines.extend(f"• {bit}" for bit in proc_bits)

    try:
        from daily_game_cycle import enabled, status_summary

        if enabled():
            st = status_summary()
            active = st.get("active_game") or "готово"
            rem = st.get("remaining") or {}
            quota = " ".join(f"{g}={rem.get(g, 0)}" for g in VOD_GAMES)
            lines.append(f"• день {st.get('day')}: активна {active}; осталось {quota}")
        else:
            lines.append("• дневной цикл выключен")
    except Exception:
        lines.append("• дневной цикл: нет данных")

    for row in rows:
        game = str(row.get("game") or "?").upper()
        inbox = int(row.get("inbox") or 0)
        actionable = int(row.get("actionable_inbox") or 0)
        exhausted = int(row.get("exhausted_inbox") or 0)
        streak = int(row.get("streak") or 0)
        daily = row.get("daily_sent")
        left = row.get("daily_quota_left")
        daily_bit = ""
        if daily is not None and left is not None:
            daily_bit = f"  день {daily}/{int(daily) + int(left)}"
        hint = str(row.get("hint") or "")
        hint_bit = f"  — {hint}" if hint and hint != "ok" else ""
        lines.append(
            f"• {game}: inbox={inbox} готово={actionable} "
            f"исчерпано={exhausted} серия нулей={streak}{daily_bit}{hint_bit}"
        )

    if any(int(r.get("actionable_inbox") or 0) == 0 and int(r.get("inbox") or 0) > 0 for r in rows):
        lines.append("Inbox исчерпан — напиши /reset, чтобы снова искать клипы в уже скачанных VOD.")
    lines.append("Команды: /process · /reset")
    return "\n".join(lines)


def reset_discovery_offsets(game: str) -> None:
    state = load_state(game)
    state["discovery_query_offset"] = 0
    state["discovery_search_cycle"] = 0
    state["discovery_cycle"] = 0
    save_state(game, state)


def run_reset(game: str = "all") -> str:
    games = list(VOD_GAMES) if game == "all" else [game]
    parts: list[str] = []
    total = 0
    for g in games:
        n = reset_game(g, dry_run=False)
        reset_discovery_offsets(g)
        total += n
        parts.append(f"{g}={n}")
    if total == 0:
        return (
            "🔄 Сброс: исчерпанных VOD в inbox не было.\n"
            "Поиск YouTube начнётся с первой пачки запросов."
        )
    return (
        f"🔄 Сброс inbox: {total} VOD снова в очереди ({', '.join(parts)}).\n"
        "Поиск YouTube сброшен на первую пачку запросов — бот снова ищет видео."
    )


def discovery_start_text(game: str, *, batch: int, limit: int) -> str:
    label = game.upper() if game else "VOD"
    return f"🔍 Ищу {label}: {batch} запросов × {limit} результатов на YouTube…"


def scan_start_text(game: str, vod_id: str) -> str:
    return f"⚙️ Сканирую {game.upper()} {vod_id} — ищу клипы, не только ошибки."
