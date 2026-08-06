#!/usr/bin/env python3
"""Daily multi-game VOD cycle: MLBB → PUBG → Standoff, reset at Moscow midnight."""

from __future__ import annotations

import json
import os
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
    """True when a game produced zero sends for too many runs / too long — skip for today."""
    game = game.strip().lower()
    state = load_state()
    entry = (state.get("stall") or {}).get(game) or {}
    if entry.get("force_skip"):
        return True
    zero_runs = int(entry.get("zero_runs") or 0)
    if zero_runs >= _stall_zero_runs_limit():
        return True
    since = float(entry.get("since") or 0)
    if since > 0 and (time.time() - since) >= _stall_max_sec():
        return True
    return False


def note_feed_iteration(game: str, sent_delta: int) -> dict:
    """
    Track zero-send streaks so the cycle can skip a stuck game (anti-hang).
    Returns the stall entry after update.
    """
    reset_if_new_day()
    game = game.strip().lower()
    if game not in GAME_ORDER:
        return {}
    state = load_state()
    stall = state.setdefault("stall", {})
    entry = stall.setdefault(game, {"zero_runs": 0, "since": None, "force_skip": False})
    if int(sent_delta) > 0:
        entry["zero_runs"] = 0
        entry["since"] = None
        entry["force_skip"] = False
        entry["last_send_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    else:
        entry["zero_runs"] = int(entry.get("zero_runs") or 0) + 1
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


def active_game() -> str | None:
    """Next game that still has daily quota and is not stalled. None if all done/skipped."""
    reset_if_new_day()
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
