#!/usr/bin/env python3
"""Self-heal VOD feed: clear pauses/cooldowns, reset inbox, restart supervisor."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from reset_vod_inbox_exhausted import reset_game
from daily_game_cycle import pubg_only_mode
from telegram_owner_controls import reset_discovery_offsets, running_processes
from vod_game_registry import (
    VOD_GAMES,
    inbox_video_ids,
    load_state,
    save_state,
    spec,
    trim_used_youtube_ids,
)

DEFAULT_SUPERVISOR = "/usr/local/bin/mlbb_vod_segment_feed.sh"
DEFAULT_FEED_LOG = Path("/root/data/mlbb/mlbb_vod_segment_feed.log")
DEFAULT_SUPERVISOR_LOG = Path("/root/data/mlbb/vod_only_supervisor.log")
OWNER_BATCH_LOCK = Path("/root/data/mlbb/OWNER_BATCH_RUNNING")
OWNER_BATCH_STALE_SEC = max(300, int(os.environ.get("OWNER_BATCH_STALE_SEC", "3600")))


def feed_lock_paths() -> list[Path]:
    return [Path(f"/tmp/{game}_vod_segment_feed.lock") for game in VOD_GAMES] + [
        Path("/tmp/mlbb_vod_oneoff.lock"),
    ]


def clear_feed_locks() -> list[str]:
    removed: list[str] = []
    for path in feed_lock_paths():
        try:
            if path.exists():
                path.unlink()
                removed.append(path.name)
        except OSError:
            pass
    return removed


def clear_discovery_pauses(game: str) -> bool:
    state = load_state(game)
    changed = False
    for key in (
        "discovery_pause_until",
        "discovery_last_empty_at",
        "discovery_last_empty_403",
    ):
        if key in state:
            state.pop(key, None)
            changed = True
    if changed:
        save_state(game, state)
    return changed


def bump_scan_cooldowns(game: str) -> int:
    """Clear rescan timestamps on inbox VODs so feed retries immediately."""
    inbox_ids = inbox_video_ids(game)
    if not inbox_ids:
        return 0
    state = load_state(game)
    touched = 0
    for row in state.get("vods") or []:
        vid = str(row.get("id") or "")
        if not vid or vid not in inbox_ids:
            continue
        if row.get("exhausted"):
            continue
        keys = (
            "last_scan_at",
            "last_scan_blocked",
            "last_pool_at",
            "reject_reason",
            "singles_zero_send_streak",
        )
        if any(k in row for k in keys):
            for k in keys:
                row.pop(k, None)
            touched += 1
    if touched:
        save_state(game, state)
    return touched


def reset_inbox_exhausted(game: str) -> int:
    n = reset_game(game, dry_run=False)
    reset_discovery_offsets(game)
    return n


def _log_age_sec(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def supervisor_script() -> Path:
    raw = os.environ.get("MLBB_VOD_SUPERVISOR", DEFAULT_SUPERVISOR)
    return Path(raw)


def feed_log_path() -> Path:
    raw = os.environ.get("MLBB_VOD_FEED_LOG", str(DEFAULT_FEED_LOG))
    return Path(raw)


def supervisor_log_path() -> Path:
    raw = os.environ.get("MLBB_VOD_SUPERVISOR_LOG", str(DEFAULT_SUPERVISOR_LOG))
    return Path(raw)


def _pgrep(pattern: str) -> bool:
    try:
        proc = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return proc.returncode == 0 and bool((proc.stdout or "").strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def feed_process_alive() -> bool:
    return any(
        _pgrep(pat)
        for pat in (
            "mlbb_vod_segment_feed.sh",
            "daily_cycle_runner.py",
            "shooter_vod_segment_feed.py",
            "mlbb_vod_segment_feed.py",
        )
    )


def clear_stale_owner_batch_lock() -> str | None:
    if not OWNER_BATCH_LOCK.is_file():
        return None
    try:
        age = time.time() - OWNER_BATCH_LOCK.stat().st_mtime
    except OSError:
        age = OWNER_BATCH_STALE_SEC + 1
    if age < OWNER_BATCH_STALE_SEC:
        return f"owner batch lock свежий ({int(age // 60)} мин) — не трогаю"
    try:
        OWNER_BATCH_LOCK.unlink()
    except OSError:
        return "owner batch lock — не удалось снять"
    return f"снят зависший owner batch lock ({int(age // 3600)}ч)"


def park_exhausted_inbox(game: str) -> int:
    """Move exhausted inbox mp4s to parked/ so discovery is not blocked by dead files."""
    s = spec(game)
    inbox = s.inbox()
    parked = inbox.parent / "parked"
    if not inbox.is_dir():
        return 0
    state = load_state(game)
    registry = {str(r.get("id") or ""): r for r in state.get("vods") or []}
    moved = 0
    parked.mkdir(parents=True, exist_ok=True)
    for mp4 in sorted(inbox.glob("yt_*.mp4")):
        vid = mp4.stem[3:][:11] if mp4.stem.startswith("yt_") else mp4.stem[:11]
        row = registry.get(vid) or {}
        if not row.get("exhausted"):
            continue
        dest = parked / mp4.name
        try:
            if dest.exists():
                mp4.unlink(missing_ok=True)
            else:
                mp4.rename(dest)
            moved += 1
        except OSError:
            pass
    return moved


def restart_supervisor(*, force: bool = False) -> tuple[bool, str]:
    lock_note = clear_stale_owner_batch_lock()
    if OWNER_BATCH_LOCK.is_file():
        note = lock_note or "owner batch lock — перезапуск пропущен"
        return False, note

    dead_sec = max(60, int(os.environ.get("MLBB_VOD_FEED_DEAD_SEC", "900")))
    stuck_sec = max(dead_sec, int(os.environ.get("MLBB_VOD_FEED_STUCK_SEC", "1800")))
    log_age = _log_age_sec(feed_log_path())
    alive = feed_process_alive()

    need_restart = force or not alive
    reason = "supervisor/feed не запущен" if not alive else ""

    if alive and log_age is not None:
        if log_age > stuck_sec:
            need_restart = True
            reason = f"лог feed без изменений {int(log_age // 60)} мин"
        elif log_age > dead_sec and not _pgrep("shooter_vod_segment_feed.py") and not _pgrep(
            "mlbb_vod_segment_feed.py"
        ):
            need_restart = True
            reason = f"дочерний feed не найден, лог {int(log_age // 60)} мин назад"

    if not need_restart:
        return False, "feed уже работает"

    script = supervisor_script()
    if not script.is_file():
        return False, f"нет supervisor: {script}"

    for pat in (
        "mlbb_vod_segment_feed.py",
        "shooter_vod_segment_feed.py",
        "daily_cycle_runner.py",
        "mlbb_vod_segment_feed.sh",
    ):
        subprocess.run(["pkill", "-9", "-f", pat], check=False, timeout=5)
    time.sleep(1.5)
    clear_feed_locks()

    sup_log = supervisor_log_path()
    sup_log.parent.mkdir(parents=True, exist_ok=True)
    with sup_log.open("a", encoding="utf-8") as fh:
        proc = subprocess.Popen(
            ["nohup", str(script)],
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    time.sleep(0.8)
    if proc.poll() is not None and proc.returncode not in (None, 0):
        return False, f"supervisor exit={proc.returncode}"
    if feed_process_alive():
        return True, reason or "supervisor перезапущен"
    return True, f"{reason or 'supervisor запущен'} (ожидаем feed…)"


def trim_discovery_used_ids(game: str) -> int:
    """Clear bloated used_youtube_ids so YouTube search can find fresh VODs."""
    state = load_state(game)
    removed = trim_used_youtube_ids(state, game, aggressive=True)
    if removed:
        save_state(game, state)
    return removed


def should_auto_heal(game: str, state: dict | None = None) -> tuple[bool, str]:
    if os.environ.get("SHOOTER_VOD_AUTO_HEAL", "1") != "1":
        return False, "disabled"
    state = state if state is not None else load_state(game)
    from vod_game_registry import streak_from_state

    streak = streak_from_state(state)
    streak_need = max(2, int(os.environ.get("SHOOTER_VOD_AUTO_HEAL_STREAK", "3")))
    used_n = len(state.get("used_youtube_ids") or [])
    used_max = max(50, int(os.environ.get("SHOOTER_VOD_USED_IDS_MAX", "200")))
    pause_until = float(state.get("discovery_pause_until") or 0)
    paused = pause_until > time.time()

    if used_n > used_max:
        return True, f"used_ids={used_n}>{used_max}"
    if streak >= streak_need and paused:
        return True, f"streak={streak}+discovery_paused"
    if streak >= streak_need * 2:
        return True, f"streak={streak}"
    inbox_ids = inbox_video_ids(game)
    if inbox_ids:
        registry = {str(r.get("id") or ""): r for r in state.get("vods") or []}
        if all(registry.get(vid, {}).get("exhausted") for vid in inbox_ids):
            return True, "inbox_all_exhausted"
    return False, ""


def auto_heal_stalled_feed(game: str) -> dict[str, object]:
    """Lightweight self-heal on each feed tick — no supervisor restart."""
    state = load_state(game)
    ok, reason = should_auto_heal(game, state)
    if not ok:
        return {"healed": 0}
    stats: dict[str, object] = {"healed": 1, "reason": reason}
    stats["paused"] = int(clear_discovery_pauses(game))
    stats["trimmed"] = trim_discovery_used_ids(game)
    stats["parked"] = park_exhausted_inbox(game)
    stats["cooled"] = bump_scan_cooldowns(game)
    stats["reset"] = reset_inbox_exhausted(game)
    return stats


def _pool_ready_inbox_count(game: str) -> int:
    """Inbox VODs with cached peaks (montage can skip dense probe)."""
    state = load_state(game)
    registry = {str(r.get("id") or ""): r for r in state.get("vods") or []}
    ready = 0
    for vid in inbox_video_ids(game):
        row = registry.get(vid) or {}
        if row.get("exhausted"):
            continue
        if len(row.get("last_pool_peaks") or []) >= 2:
            ready += 1
    return ready


def _recover_games(game: str) -> list[str]:
    if game != "all":
        return [game]
    if pubg_only_mode():
        return ["pubg"]
    return list(VOD_GAMES)


def _eta_target_games(game: str) -> list[str]:
    if game != "all":
        return [game]
    if pubg_only_mode():
        return ["pubg"]
    from vod_pipeline_health import health_row

    return [
        g
        for g in VOD_GAMES
        if int(health_row(g).get("inbox") or 0) > 0
        or int(health_row(g).get("actionable_inbox") or 0) > 0
    ] or ["pubg"]


def estimate_video_wait_eta(game: str = "pubg") -> str:
    """Conservative ETA for the next Telegram clip after /recover."""
    from vod_pipeline_health import health_row

    targets = _eta_target_games(game)
    lines: list[str] = []
    feed_ok = feed_process_alive()
    log_age = _log_age_sec(feed_log_path())

    for g in targets:
        row = health_row(g)
        actionable = int(row.get("actionable_inbox") or 0)
        inbox = int(row.get("inbox") or 0)
        streak = int(row.get("streak") or 0)
        pool_ready = _pool_ready_inbox_count(g)
        label = g.upper()

        if feed_ok and log_age is not None and log_age < 180:
            eta = "~10–25 мин (склейка сейчас в работе)"
        elif actionable == 0 and inbox > 0:
            eta = "~30–60 мин после /reset pubg (inbox исчерпан)"
        elif pool_ready > 0:
            if streak >= 4:
                eta = f"~20–45 мин ({pool_ready} VOD с пиками, недавно были отказы)"
            else:
                eta = f"~15–30 мин ({pool_ready} VOD с готовыми пиками)"
        elif actionable > 0:
            eta = f"~40–70 мин (поиск боёв в {actionable} VOD)"
        elif not feed_ok:
            eta = "~5–20 мин (feed перезапускается)"
        else:
            eta = "~20–40 мин (поиск новых VOD на YouTube)"
        lines.append(f"⏱ {label}: ожидайте первое видео {eta}")

    return "\n".join(lines)


def run_recover(
    game: str = "all",
    *,
    restart: Callable[..., tuple[bool, str]] = restart_supervisor,
    probe: Callable[[], dict[str, bool]] = running_processes,
    force_send: bool = True,
) -> str:
    games = _recover_games(game)
    lock_note = clear_stale_owner_batch_lock()
    locks = clear_feed_locks()
    pauses = 0
    cooled = 0
    reset_total = 0
    parked = 0
    trimmed_used = 0
    for g in games:
        if clear_discovery_pauses(g):
            pauses += 1
        cooled += bump_scan_cooldowns(g)
        reset_total += reset_inbox_exhausted(g)
        parked += park_exhausted_inbox(g)
        trimmed_used += trim_discovery_used_ids(g)
        if g == "pubg":
            state = load_state(g)
            if state.pop("pubg_singles_active_vod", None):
                save_state(g, state)

    send_results: list[dict[str, object]] = []
    if force_send and os.environ.get("VOD_RECOVER_FORCE_SEND", "1") == "1":
        from vod_force_send import force_send_game

        for g in games:
            send_results.append(
                force_send_game(
                    g,
                    stop_running=True,
                    timeout_sec=max(
                        120,
                        int(os.environ.get("VOD_RECOVER_FORCE_SEND_TIMEOUT_SEC", "1200")),
                    ),
                )
            )

    restarted, restart_note = restart(force=True)

    running = probe()
    lines = ["🔧 Восстановление VOD feed"]
    if lock_note:
        lines.append(f"• owner lock: {lock_note}")
    if parked:
        lines.append(f"• parked: убрано исчерпанных из inbox {parked}")
    if locks:
        lines.append(f"• lock: снято {len(locks)} ({', '.join(locks[:4])}{'…' if len(locks) > 4 else ''})")
    else:
        lines.append("• lock: не было")
    lines.append(f"• discovery pause: сброшено игр {pauses}/{len(games)}")
    lines.append(f"• cooldown scan: разблокировано VOD {cooled}")
    if reset_total:
        lines.append(f"• inbox exhausted: снова в очереди {reset_total} VOD")
    else:
        lines.append("• inbox exhausted: изменений не было")
    if trimmed_used:
        lines.append(f"• discovery: очищено used YouTube ID {trimmed_used}")
    else:
        lines.append("• discovery: used YouTube ID без изменений")
    if send_results:
        any_sent = False
        for row in send_results:
            g = str(row.get("game") or "?").upper()
            sent = int(row.get("sent") or 0)
            if sent > 0:
                any_sent = True
                lines.append(f"• отправка {g}: {sent} клип(ов) ✅")
            else:
                hint = str(row.get("hint") or row.get("flags") or row.get("error") or "").strip()
                lines.append(f"• отправка {g}: 0 — {hint or 'гейты не прошли'}")
        if any_sent:
            lines.append("Видео должно прийти в этот чат в течение ~1 мин.")
        else:
            lines.append(estimate_video_wait_eta(game))
    else:
        lines.append(estimate_video_wait_eta(game))
    lines.append(f"• supervisor: {'перезапущен' if restarted else 'без перезапуска'} — {restart_note}")
    lines.append(
        "• процессы: "
        + ", ".join(
            f"{name}={'ok' if running.get(name) else 'нет'}"
            for name in ("vod_supervisor", "daily_cycle", "shooter_feed", "telegram_bot")
        )
    )
    if not send_results or not any(int(r.get("sent") or 0) > 0 for r in send_results):
        lines.append("Если снова тишина — /reset pubg или кнопка «Отправить сейчас».")
    return "\n".join(lines)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Recover stalled VOD feed (CLI)")
    parser.add_argument("--game", default="all", choices=("all", *VOD_GAMES))
    args = parser.parse_args()
    print(run_recover(args.game))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
