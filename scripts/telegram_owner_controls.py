#!/usr/bin/env python3
"""Owner Telegram controls: process status and exhausted-inbox reset (text commands)."""

from __future__ import annotations

import subprocess
from typing import Iterable

from reset_vod_inbox_exhausted import reset_game
from vod_game_registry import VOD_GAMES, VOD_PIPELINE_REV, load_state, save_state
from vod_pipeline_health import health_row

# Inline callback ids for owner control buttons.
CALLBACK_RESET = "ops_reset"
CALLBACK_PROCESS = "ops_process"
CALLBACK_RECOVER = "ops_recover"
CALLBACK_SEND_NOW = "ops_send_now"
CALLBACK_HANG_AGENT = "ops_hang_agent"

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

DEFAULT_VOD_SEARCH_LIMIT = 80
DEFAULT_VOD_SEARCH_BATCH = 10


BTN_HANG_AGENT = "🤖 Агент зависания"
BTN_PROCESS = "📊 Процесс"
BTN_RECOVER = "🔧 Recover"
BTN_SEND_NOW = "📤 Отправить"
BTN_RESET = "🔄 Сброс"


def owner_controls_keyboard() -> dict:
    """Inline buttons under owner status / recover replies."""
    return {
        "inline_keyboard": [
            [
                {"text": BTN_HANG_AGENT, "callback_data": CALLBACK_HANG_AGENT},
            ],
            [
                {"text": BTN_PROCESS, "callback_data": CALLBACK_PROCESS},
                {"text": BTN_RECOVER, "callback_data": CALLBACK_RECOVER},
            ],
            [
                {"text": BTN_SEND_NOW, "callback_data": CALLBACK_SEND_NOW},
                {"text": BTN_RESET, "callback_data": CALLBACK_RESET},
            ],
        ],
    }


def owner_reply_keyboard() -> dict:
    """Persistent bottom keyboard — always visible for the owner."""
    return {
        "keyboard": [
            [{"text": BTN_HANG_AGENT}],
            [{"text": BTN_PROCESS}, {"text": BTN_RECOVER}],
            [{"text": BTN_SEND_NOW}, {"text": BTN_RESET}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def _norm_text(text: str) -> str:
    return " ".join((text or "").strip().split()).lower()


def _btn_norm(text: str) -> str:
    """Normalize reply-keyboard presses (strip leading emoji / symbols)."""
    import re

    return re.sub(r"^[^a-zа-яё0-9/]+", "", _norm_text(text), flags=re.IGNORECASE)


def is_process_command(text: str) -> bool:
    raw = (text or "").strip()
    token = raw.split()[0].split("@")[0].lower() if raw else ""
    if token in ("/process", "/процесс", "/proc"):
        return True
    return _btn_norm(raw) in {
        "процесс",
        "process",
        "статус пайплайна",
    }


def is_reset_command(text: str) -> bool:
    raw = (text or "").strip()
    token = raw.split()[0].split("@")[0].lower() if raw else ""
    if token in ("/reset", "/сброс"):
        return True
    return _btn_norm(raw) in {
        "сброс",
        "reset",
        "сброс процесса",
        "reset process",
    }


def is_recover_command(text: str) -> bool:
    raw = (text or "").strip()
    token = raw.split()[0].split("@")[0].lower() if raw else ""
    if token in ("/recover", "/восстановить", "/fix"):
        return True
    return _btn_norm(raw) in {
        "recover",
        "восстановить",
        "восстановление",
        "почему нет видео",
        "нет видео",
    }


def is_send_now_command(text: str) -> bool:
    raw = (text or "").strip()
    token = raw.split()[0].split("@")[0].lower() if raw else ""
    if token in ("/send", "/отправить", "/sendnow"):
        return True
    return _btn_norm(raw) in {
        "отправить",
        "send",
        "send now",
        "отправка",
    }


def is_hang_agent_command(text: str) -> bool:
    raw = (text or "").strip()
    token = raw.split()[0].split("@")[0].lower() if raw else ""
    if token in ("/agent", "/hang", "/завис", "/агент"):
        return True
    return _btn_norm(raw) in {
        "агент",
        "агент зависания",
        "завис",
        "снова завис",
        "hang agent",
        "hang",
    }


def parse_reset_game(text: str) -> str:
    return parse_game_arg(text, default="all")


def parse_recover_game(text: str) -> str:
    return parse_game_arg(text, default="all")


def parse_game_arg(text: str, *, default: str = "all") -> str:
    parts = (text or "").strip().split()
    if len(parts) < 2:
        return default
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

    lines = ["📊 Процесс пайплайна", f"• rev: {VOD_PIPELINE_REV}"]
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
            mode = "PUBG-only ∞" if st.get("pubg_only") else "мульти-игра"
            lines.append(f"• день {st.get('day')}: {mode}; активна {active}; осталось {quota}")
        else:
            lines.append("• дневной цикл выключен")
    except Exception:
        lines.append("• дневной цикл: нет данных")

    for row in rows:
        game = str(row.get("game") or "?").upper()
        if game == "PUBG":
            try:
                st = load_state("pubg")
                used_n = len(st.get("used_youtube_ids") or [])
                pause = float(st.get("discovery_pause_until") or 0)
                import time as _time

                if pause > _time.time():
                    lines.append(f"• discovery pause: ещё {int(pause - _time.time())}s")
                lines.append(f"• used YouTube IDs: {used_n}")
            except Exception:
                pass
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
        lines.append("Inbox исчерпан — /reset или /recover pubg.")
    if not running.get("vod_supervisor") or not (
        running.get("daily_cycle") or running.get("shooter_feed") or running.get("mlbb_feed")
    ):
        lines.append("Feed не работает — напиши /recover или жми «Агент зависания».")
    lines.append("Команды: /agent · /process · /recover · /reset · кнопки ниже")
    return "\n".join(lines)


def reset_discovery_offsets(game: str) -> None:
    state = load_state(game)
    state["discovery_query_offset"] = 0
    state["discovery_search_cycle"] = 0
    state["discovery_cycle"] = 0
    save_state(game, state)


def run_recover(game: str = "all") -> str:
    from vod_feed_recover import run_recover as _run_recover

    return _run_recover(game)


def run_hang_agent(game: str = "pubg") -> str:
    """One-button hang agent: diagnose → clear stale locks → recover → force-send.

    This is the local playbook the cloud agent runs when the owner says «завис».
    """
    import os
    import time
    from pathlib import Path

    target = "pubg" if game in ("all", "", "pubg") else game.strip().lower()
    lines = [f"🤖 Агент зависания ({target})"]

    lock = Path(os.environ.get("VOD_HANG_RECOVER_LOCK", "/tmp/vod_hang_recover.lock"))
    if lock.is_file():
        raw = lock.read_text(encoding="utf-8", errors="ignore").strip()
        stale = True
        try:
            pid = int(raw.split()[0])
            os.kill(pid, 0)
            stale = False
            lines.append(f"• recover уже идёт (pid={pid})")
        except (ValueError, OSError, IndexError):
            stale = True
        if stale:
            try:
                lock.unlink()
                lines.append("• снял зависший recover-lock")
            except OSError as exc:
                lines.append(f"• lock: {exc}")

    try:
        from vod_hang_detector import (
            apply_agent_recover_env,
            auto_unload_and_recover,
            detect_hang,
        )

        report = detect_hang()
        age = int(report.last_send_age_sec or 0)
        hb = int(report.heartbeat_age_sec or 0) if report.heartbeat_age_sec else -1
        lines.append(
            f"• диагноз: {'OK' if report.ok else 'HANG'} | "
            f"тишина {age // 60}м | hb={hb}s | "
            f"{', '.join(report.reasons[:3]) or '—'}"
        )
        heal = auto_unload_and_recover(report, game=target, force=True, background=False)
        lines.append(
            f"• heal: {heal.get('action')} "
            f"({' '.join(str(a) for a in (heal.get('actions') or [])[:4]) or '—'})"
        )
        apply_agent_recover_env(os.environ, escalation=int(heal.get("escalation") or 0))
    except Exception as exc:  # noqa: BLE001
        lines.append(f"• heal error: {exc}")

    try:
        from vod_force_send import force_send, format_force_send_report

        t0 = time.time()
        report_rows = force_send(target)
        report_txt = format_force_send_report(report_rows)
        lines.append(report_txt)
        lines.append(f"• force-send за {int(time.time() - t0)}с")
        mined = (
            "mined_out" in report_txt
            or "не отправлено" in report_txt
            or "sent=0" in report_txt.lower()
        )
        if mined:
            lines.append("• mined/пусто — unload + reset + unpark и шлю ещё раз")
            try:
                from vod_hang_detector import unload_stuck_inbox_vod

                unloaded = unload_stuck_inbox_vod(target, min_rejects=2)
                if unloaded:
                    lines.append(f"• unload stuck VOD: {unloaded}")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"• unload: {exc}")
            try:
                lines.append(run_reset(target))
            except Exception as exc:  # noqa: BLE001
                lines.append(f"• reset: {exc}")
            try:
                from vod_feed_recover import unpark_ready_vods

                n = unpark_ready_vods(target, limit=5)
                lines.append(f"• unpark: {n}")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"• unpark: {exc}")
            # Escalate soften one step for the retry pass.
            try:
                from vod_hang_detector import apply_agent_recover_env, _heal_escalation

                apply_agent_recover_env(
                    os.environ,
                    escalation=min(2, int(_heal_escalation()) + 1),
                )
            except Exception:
                pass
            report_rows = force_send(target)
            lines.append(format_force_send_report(report_rows))
    except Exception as exc:  # noqa: BLE001
        lines.append(f"• force-send error: {exc}")
        try:
            lines.append(run_recover(target))
        except Exception as exc2:  # noqa: BLE001
            lines.append(f"• recover fallback: {exc2}")

    lines.append(
        "Готово. Автоагент тоже следит каждые 5 мин — кнопки ниже на всякий случай."
    )
    return "\n".join(lines)


def run_send_now(game: str = "all") -> str:
    from vod_force_send import force_send, format_force_send_report

    return format_force_send_report(force_send(game))


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
