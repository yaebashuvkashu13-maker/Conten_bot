#!/usr/bin/env python3
"""Daily multi-game VOD cycle: MLBB → PUBG → Standoff, reset at Moscow midnight."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

DATA_ROOT = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))


def _state_path() -> Path:
    return Path(os.environ.get("DAILY_GAME_CYCLE_STATE", str(DATA_ROOT / "daily_game_cycle.json")))

GAME_ORDER = ("mlbb", "pubg", "standoff", "genshin", "wot")
GAME_PROFILE = {
    "mlbb": "mobile_legends",
    "pubg": "pubg",
    "standoff": "standoff",
    "genshin": "genshin",
    "wot": "wot",
}


def _today_key() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def enabled() -> bool:
    return os.environ.get("DAILY_GAME_CYCLE_ENABLED", "0") == "1"


def quota_for(game: str) -> int:
    game = game.strip().lower()
    defaults = {"mlbb": 5, "pubg": 3, "standoff": 3, "genshin": 5, "wot": 3}
    primary = f"DAILY_GAME_{game.upper()}_QUOTA"
    legacy = f"DAILY_{game.upper()}_QUOTA"
    raw = os.environ.get(primary, os.environ.get(legacy, str(defaults.get(game, 5))))
    try:
        return max(0, int(raw))
    except ValueError:
        return defaults.get(game, 5)


def load_state() -> dict:
    from vod_state_io import load_json_state

    path = _state_path()

    def _default() -> dict:
        return {"day": _today_key(), "sends": {g: 0 for g in GAME_ORDER}, "notified": {}}

    data = load_json_state(path, _default)
    data.setdefault("day", _today_key())
    data.setdefault("sends", {})
    data.setdefault("notified", {})
    for g in GAME_ORDER:
        data["sends"].setdefault(g, 0)
    return data


def save_state(state: dict) -> None:
    from vod_state_io import save_json_state

    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json_state(_state_path(), state)


def reset_if_new_day() -> bool:
    """Reset counters when calendar day changes. Returns True if reset happened."""
    state = load_state()
    today = _today_key()
    if state.get("day") == today:
        return False
    state = {
        "day": today,
        "sends": {g: 0 for g in GAME_ORDER},
        "notified": {},
        "stall": {},
        "reset_from": state.get("day"),
    }
    save_state(state)
    return True


def send_count(game: str) -> int:
    reset_if_new_day()
    return int(load_state().get("sends", {}).get(game.strip().lower(), 0))


def quota_remaining(game: str) -> int:
    game = game.strip().lower()
    return max(0, quota_for(game) - send_count(game))


def record_send(game: str, count: int = 1) -> None:
    reset_if_new_day()
    game = game.strip().lower()
    if game not in GAME_ORDER:
        return
    state = load_state()
    sends = state.setdefault("sends", {})
    sends[game] = int(sends.get(game, 0)) + count
    save_state(state)


def _stall_zero_runs_limit() -> int:
    try:
        return max(2, int(os.environ.get("DAILY_GAME_STALL_ZERO_RUNS", "6")))
    except ValueError:
        return 6


def _stall_max_sec() -> float:
    try:
        return max(300.0, float(os.environ.get("DAILY_GAME_STALL_MAX_SEC", "2700")))
    except ValueError:
        return 2700.0


def is_game_stalled(game: str) -> bool:
    """True when a game is force-skipped or stuck with no progress for the wall-clock budget.

    Zero-run count alone must NOT skip: shooter feed processes one VOD per iteration,
    so a healthy inbox of 10+ files would falsely stall after N rejects.
    """
    game = game.strip().lower()
    state = load_state()
    entry = (state.get("stall") or {}).get(game) or {}
    if entry.get("force_skip"):
        return True
    since = float(entry.get("since") or 0)
    if since > 0 and (time.time() - since) >= _stall_max_sec():
        return True
    # Fast thrash (inbox_dead / discovery hang): many quick zero runs + min age.
    zero_runs = int(entry.get("zero_runs") or 0)
    thrash = int(entry.get("thrash_runs") or 0)
    if thrash >= _stall_zero_runs_limit() and since > 0 and (time.time() - since) >= min(300.0, _stall_max_sec() / 2):
        return True
    if zero_runs >= max(20, _stall_zero_runs_limit() * 4) and since > 0 and (time.time() - since) >= _stall_max_sec():
        return True
    return False


def note_feed_iteration(
    game: str,
    sent_delta: int,
    *,
    thrash: bool = False,
    timed_out: bool = False,
) -> dict:
    """
    Track zero-send streaks so the cycle can skip a stuck game (anti-hang).
    thrash=True when feed returned immediately with inbox_dead / no candidates
    (not a normal one-VOD reject).
    timed_out=True after runner killed a hung feed — counted separately so
    self-heal unstall cannot reset the timeout streak forever.
    """
    reset_if_new_day()
    game = game.strip().lower()
    if game not in GAME_ORDER:
        return {}
    state = load_state()
    stall = state.setdefault("stall", {})
    entry = stall.setdefault(
        game,
        {"zero_runs": 0, "thrash_runs": 0, "timeout_runs": 0, "since": None, "force_skip": False},
    )
    if int(sent_delta) > 0:
        entry["zero_runs"] = 0
        entry["thrash_runs"] = 0
        entry["timeout_runs"] = 0
        entry["since"] = None
        entry["force_skip"] = False
        entry["last_send_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    else:
        entry["zero_runs"] = int(entry.get("zero_runs") or 0) + 1
        if thrash:
            entry["thrash_runs"] = int(entry.get("thrash_runs") or 0) + 1
        if timed_out:
            entry["timeout_runs"] = int(entry.get("timeout_runs") or 0) + 1
            entry["last_timeout_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if not entry.get("since"):
            entry["since"] = time.time()
        entry["last_zero_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    stall[game] = entry
    save_state(state)
    return dict(entry)


def force_skip_game(game: str, reason: str = "manual") -> None:
    """Skip remaining quota for a game today (e.g. inbox dead + discovery broken)."""
    reset_if_new_day()
    game = game.strip().lower()
    if game not in GAME_ORDER:
        return
    state = load_state()
    stall = state.setdefault("stall", {})
    entry = stall.setdefault(game, {"zero_runs": 0, "since": time.time(), "force_skip": False})
    entry["force_skip"] = True
    entry["skip_reason"] = reason[:160]
    entry["skipped_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # Push zero_runs past limit so is_game_stalled stays true even if force_skip cleared.
    entry["zero_runs"] = max(int(entry.get("zero_runs") or 0), _stall_zero_runs_limit())
    if not entry.get("since"):
        entry["since"] = time.time()
    stall[game] = entry
    save_state(state)


def clear_stall(game: str | None = None, *, reason: str = "manual_clear") -> list[str]:
    """Clear force_skip / zero-run stall so remaining quota can resume."""
    reset_if_new_day()
    state = load_state()
    stall = state.setdefault("stall", {})
    games = [game.strip().lower()] if game else list(GAME_ORDER)
    cleared: list[str] = []
    for g in games:
        if g not in GAME_ORDER:
            continue
        entry = stall.setdefault(g, {})
        # Do not wipe an active timeout streak via self-heal — that reopened the TG spam loop.
        if int(entry.get("timeout_runs") or 0) >= 2 and "self_heal" in str(reason):
            continue
        if not (
            entry.get("force_skip")
            or int(entry.get("zero_runs") or 0) > 0
            or int(entry.get("thrash_runs") or 0) > 0
            or int(entry.get("timeout_runs") or 0) > 0
            or entry.get("since")
        ):
            continue
        entry["force_skip"] = False
        entry["zero_runs"] = 0
        entry["thrash_runs"] = 0
        # Keep timeout_runs unless explicit manual clear / new day.
        if "self_heal" not in str(reason):
            entry["timeout_runs"] = 0
        entry["since"] = None
        entry["cleared_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["clear_reason"] = reason[:160]
        entry.pop("skip_reason", None)
        stall[g] = entry
        cleared.append(g)
    notified = state.setdefault("notified", {})
    for key in list(notified):
        if key.startswith("all_stalled:") or key.startswith("stall_skip:"):
            del notified[key]
    save_state(state)
    return cleared


def _game_data_root(game: str) -> Path:
    g = game.strip().upper()
    for key in (f"VOD_{g}_DATA_ROOT", f"SHOOTER_{g}_DATA_ROOT", f"{g}_DATA_ROOT"):
        raw = os.environ.get(key)
        if raw:
            return Path(raw)
    return Path(f"/root/data/{game.strip().lower()}")


def _ffprobe_duration_quick(path: Path) -> float:
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
        return float(out.decode().strip() or 0)
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0


def _game_min_usable_sec(game: str) -> float:
    g = game.strip().lower()
    if g == "mlbb":
        try:
            return float(os.environ.get("MLBB_VOD_MIN_SEC", "480"))
        except ValueError:
            return 480.0
    try:
        base = float(os.environ.get("SHOOTER_VOD_MIN_SEC") or "120")
    except ValueError:
        base = 120.0
    if os.environ.get("SHOOTER_VOD_MONTAGE", "1") == "1":
        try:
            floor = float(os.environ.get("SHOOTER_VOD_MONTAGE_MIN_VOD_SEC", "120"))
        except ValueError:
            floor = 120.0
        return max(base, floor)
    return base


def _game_inbox_ready(game: str) -> bool:
    """True when local inbox OR parked has at least one VOD long enough to scan."""
    game = game.strip().lower()
    root = _game_data_root(game) / "youtube_nightly"
    roots = [
        root / "inbox",
        root / "parked",
        Path(f"/root/data/{game}/youtube_nightly/inbox"),
        Path(f"/root/data/{game}/youtube_nightly/parked"),
        Path(f"/root/data/{game}/inbox"),
    ]
    if game == "genshin":
        roots.append(Path("/root/data/genshin/remount"))
    min_sec = _game_min_usable_sec(game)
    # Fast path: any sufficiently large file is likely long enough (avoid ffprobe storm).
    # ~40MB floor ≈ short junk; real 10min+ VODs are usually much larger.
    size_floor = int(os.environ.get("DAILY_GAME_USABLE_SIZE_FLOOR", "80000000"))
    for folder in roots:
        try:
            for mp4 in list(folder.glob("yt_*.mp4")) + list(folder.glob("*.mp4")):
                try:
                    if mp4.stat().st_size < size_floor:
                        # Still verify a few small-looking files — some are long low-bitrate.
                        if _ffprobe_duration_quick(mp4) >= min_sec:
                            return True
                        continue
                    # Large file: confirm duration once.
                    if _ffprobe_duration_quick(mp4) >= min_sec:
                        return True
                except OSError:
                    continue
        except OSError:
            continue
    return False


def game_has_ready_media(game: str) -> bool:
    """Public alias for runner / heal — usable local VOD present."""
    return _game_inbox_ready(game)


def unstall_games_with_inbox() -> list[str]:
    """
    Stall-skip must not freeze remaining quota for hours while local VODs sit unused.
    If a stalled game still has inbox/parked files, clear its stall once.
    """
    if os.environ.get("DAILY_GAME_UNSTALL_ON_INBOX", "1") != "1":
        return []
    reset_if_new_day()
    cleared: list[str] = []
    for game in GAME_ORDER:
        if quota_remaining(game) <= 0:
            continue
        if not is_game_stalled(game):
            continue
        if not _game_inbox_ready(game):
            continue
        cleared.extend(clear_stall(game, reason="inbox_ready_unstall"))
    return cleared


def active_game() -> str | None:
    """Next game that still has daily quota and is not stalled. None if all done/skipped."""
    reset_if_new_day()
    unstall_games_with_inbox()
    for game in GAME_ORDER:
        if quota_remaining(game) <= 0:
            continue
        if is_game_stalled(game):
            continue
        return game
    return None


def can_send_for_game(game: str, count: int = 1) -> tuple[bool, str]:
    if not enabled():
        return True, "cycle_disabled"
    reset_if_new_day()
    game = game.strip().lower()
    if is_game_stalled(game):
        return False, f"{game}_stalled"
    active = active_game()
    if active is None:
        return False, "all_quotas_done"
    if game != active:
        return False, f"wait_for_{active}"
    if quota_remaining(game) < count:
        return False, f"{game}_quota_done"
    return True, "ok"


def profile_for_game(game: str) -> str:
    return GAME_PROFILE.get(game.strip().lower(), game)


def status_summary() -> dict:
    reset_if_new_day()
    state = load_state()
    sends = {g: int(state.get("sends", {}).get(g, 0)) for g in GAME_ORDER}
    quotas = {g: quota_for(g) for g in GAME_ORDER}
    return {
        "day": state.get("day"),
        "active_game": active_game(),
        "sends": sends,
        "quotas": quotas,
        "remaining": {g: max(0, quotas[g] - sends[g]) for g in GAME_ORDER},
    }


def mark_notified(key: str) -> None:
    state = load_state()
    notified = state.setdefault("notified", {})
    notified[key] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)


def was_notified(key: str) -> bool:
    return key in load_state().get("notified", {})
