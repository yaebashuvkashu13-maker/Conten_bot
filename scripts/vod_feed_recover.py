#!/usr/bin/env python3
"""Self-heal VOD feed: clear pauses/cooldowns, reset inbox, restart supervisor."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from reset_vod_inbox_exhausted import reset_game
from telegram_owner_controls import reset_discovery_offsets, running_processes
from vod_game_registry import VOD_GAMES, inbox_video_ids, load_state, save_state, spec

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


def run_recover(
    game: str = "all",
    *,
    restart: Callable[..., tuple[bool, str]] = restart_supervisor,
    probe: Callable[[], dict[str, bool]] = running_processes,
) -> str:
    games = list(VOD_GAMES) if game == "all" else [game]
    lock_note = clear_stale_owner_batch_lock()
    locks = clear_feed_locks()
    pauses = 0
    cooled = 0
    reset_total = 0
    parked = 0
    for g in games:
        if clear_discovery_pauses(g):
            pauses += 1
        cooled += bump_scan_cooldowns(g)
        reset_total += reset_inbox_exhausted(g)
        parked += park_exhausted_inbox(g)

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
    lines.append(f"• supervisor: {'перезапущен' if restarted else 'без перезапуска'} — {restart_note}")
    lines.append(
        "• процессы: "
        + ", ".join(
            f"{name}={'ok' if running.get(name) else 'нет'}"
            for name in ("vod_supervisor", "daily_cycle", "shooter_feed", "telegram_bot")
        )
    )
    lines.append("Через 1–2 мин проверь /process. Если снова тишина — /reset pubg.")
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
