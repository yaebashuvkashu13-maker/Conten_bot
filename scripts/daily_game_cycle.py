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
    defaults = {"mlbb": 10, "pubg": 10, "standoff": 10, "genshin": 5, "wot": 5}
    env_key = f"DAILY_{game.upper()}_QUOTA"
    fallback = f"DAILY_GAME_{game.upper()}_QUOTA"
    raw = os.environ.get(env_key, os.environ.get(fallback, str(defaults.get(game, 10))))
    try:
        return max(0, int(raw))
    except ValueError:
        return defaults.get(game, 10)


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


def active_game() -> str | None:
    """Next game that still has daily quota. None if all quotas met."""
    reset_if_new_day()
    for game in GAME_ORDER:
        if quota_remaining(game) > 0:
            return game
    return None


def can_send_for_game(game: str, count: int = 1) -> tuple[bool, str]:
    if not enabled():
        return True, "cycle_disabled"
    reset_if_new_day()
    game = game.strip().lower()
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


def start_next_quota_now(*, notify: bool = False) -> dict:
    """
    Owner override: fresh daily quotas immediately (do not wait for MSK midnight).
    Resets send counters and clears active-game notification latch.
    """
    state = load_state()
    prev_day = str(state.get("day") or "")
    prev_sends = dict(state.get("sends") or {})
    state = {
        "day": _today_key(),
        "sends": {g: 0 for g in GAME_ORDER},
        "notified": {},
        "reset_from": prev_day,
        "forced_early_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prev_sends": prev_sends,
    }
    save_state(state)
    summary = status_summary()
    if notify:
        token = os.environ.get("TG_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TG_CHAT_ID", "").strip()
        if token and chat_id:
            try:
                from mlbb_vod_segment_feed import send_message

                send_message(
                    token,
                    chat_id,
                    "🔄 Досрочный старт дневной квоты\n"
                    f"Активна: {summary.get('active_game', '?')}\n"
                    f"Осталось: {summary.get('remaining', {})}",
                )
            except Exception:
                pass
    return summary
