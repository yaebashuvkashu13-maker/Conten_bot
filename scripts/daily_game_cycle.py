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
    defaults = {"mlbb": 5, "pubg": 5, "standoff": 5, "genshin": 5, "wot": 5}
    # DAILY_GAME_* is canonical; DAILY_* kept for backward compatibility.
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


def record_discovery_miss(game: str) -> int:
    """
    Count consecutive discovery misses for a game.
    Returns the new miss streak for that game today.
    """
    reset_if_new_day()
    game = game.strip().lower()
    if game not in GAME_ORDER:
        return 0
    state = load_state()
    misses = state.setdefault("discovery_misses", {})
    # Reset other games' streaks so only the stuck game accumulates.
    for g in list(misses.keys()):
        if g != game:
            misses[g] = 0
    misses[game] = int(misses.get(game, 0)) + 1
    state["discovery_misses"] = misses
    save_state(state)
    return int(misses[game])


def clear_discovery_miss(game: str | None = None) -> None:
    reset_if_new_day()
    state = load_state()
    misses = state.setdefault("discovery_misses", {})
    if game is None:
        state["discovery_misses"] = {g: 0 for g in GAME_ORDER}
    else:
        misses[game.strip().lower()] = 0
    save_state(state)


def skip_game_quota(game: str, *, reason: str = "discovery_miss") -> None:
    """Mark game quota as filled for today so the cycle advances."""
    reset_if_new_day()
    game = game.strip().lower()
    if game not in GAME_ORDER:
        return
    state = load_state()
    sends = state.setdefault("sends", {})
    need = quota_for(game)
    sends[game] = max(int(sends.get(game, 0)), need)
    skipped = state.setdefault("skipped", {})
    skipped[game] = {"reason": reason, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    # Clear miss streak after skip.
    misses = state.setdefault("discovery_misses", {})
    misses[game] = 0
    save_state(state)


def discovery_miss_skip_after() -> int:
    return max(1, int(os.environ.get("DAILY_GAME_DISCOVERY_MISS_SKIP", "3")))


def _other_games_have_quota(game: str) -> bool:
    game = game.strip().lower()
    for g in GAME_ORDER:
        if g == game:
            continue
        if quota_remaining(g) > 0:
            return True
    return False


def maybe_skip_on_discovery_miss(game: str) -> bool:
    """
    After N consecutive discovery misses, skip this game's remaining quota.
    Never skips the last game that still has quota (avoids idle-until-midnight).
    Returns True if the game was skipped.
    """
    if not enabled():
        return False
    if os.environ.get("DAILY_GAME_SKIP_ON_DISCOVERY_MISS", "1") != "1":
        return False
    streak = record_discovery_miss(game)
    need = discovery_miss_skip_after()
    if streak < need:
        return False
    # Flaky YouTube 403 must not zero out the only remaining game.
    if not _other_games_have_quota(game):
        return False
    skip_game_quota(game, reason=f"discovery_miss_x{streak}")
    return True


def catchup_games() -> tuple[str, ...]:
    """Games eligible for one end-of-day catch-up after discovery_miss skip."""
    raw = os.environ.get("DAILY_GAME_CATCHUP_GAMES", "mlbb").strip()
    if not raw or raw in ("0", "none", "off"):
        return ()
    allowed = set(GAME_ORDER)
    out: list[str] = []
    for part in raw.split(","):
        g = part.strip().lower()
        if g in allowed and g not in out:
            out.append(g)
    return tuple(out)


def maybe_catchup_skipped_games() -> list[str]:
    """
    Once per day, after every game has filled its quota, reopen games that were
    skipped due to discovery_miss (default: mlbb only).

    Keeps WoT (or any still-active game) running — catch-up only triggers when
    the cycle would otherwise idle.
    """
    if not enabled():
        return []
    if os.environ.get("DAILY_GAME_CATCHUP_ON_SKIP", "1") != "1":
        return []
    candidates = catchup_games()
    if not candidates:
        return []

    state = load_state()
    if bool(state.get("catchup_done")):
        return []

    sends = state.get("sends") or {}
    all_done = all(int(sends.get(g, 0) or 0) >= quota_for(g) for g in GAME_ORDER)
    if not all_done:
        return []

    skipped = state.get("skipped") or {}
    reopen: list[str] = []
    for g in candidates:
        meta = skipped.get(g)
        if not isinstance(meta, dict):
            continue
        reason = str(meta.get("reason") or "")
        if reason.startswith("discovery_miss"):
            reopen.append(g)

    state["catchup_done"] = True
    if not reopen:
        save_state(state)
        return []

    sends = state.setdefault("sends", {})
    skipped_map = state.setdefault("skipped", {})
    misses = state.setdefault("discovery_misses", {})
    for g in reopen:
        sends[g] = 0
        skipped_map.pop(g, None)
        misses[g] = 0
    state["catchup_games"] = reopen
    state["catchup_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    return reopen


def active_game() -> str | None:
    """Next game that still has daily quota. None if all quotas met."""
    reset_if_new_day()
    for game in GAME_ORDER:
        if quota_remaining(game) > 0:
            return game
    # All quotas look filled — reopen discovery_miss skips once (e.g. MLBB after WoT).
    if maybe_catchup_skipped_games():
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
    # Resolve active (may trigger one-shot discovery_miss catch-up).
    active = active_game()
    state = load_state()
    sends = {g: int(state.get("sends", {}).get(g, 0)) for g in GAME_ORDER}
    quotas = {g: quota_for(g) for g in GAME_ORDER}
    return {
        "day": state.get("day"),
        "active_game": active,
        "sends": sends,
        "quotas": quotas,
        "remaining": {g: max(0, quotas[g] - sends[g]) for g in GAME_ORDER},
        "catchup_done": bool(state.get("catchup_done")),
        "catchup_games": list(state.get("catchup_games") or []),
        "skipped": {
            g: (state.get("skipped") or {}).get(g)
            for g in GAME_ORDER
            if (state.get("skipped") or {}).get(g)
        },
    }


def mark_notified(key: str) -> None:
    state = load_state()
    notified = state.setdefault("notified", {})
    notified[key] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)


def was_notified(key: str) -> bool:
    return key in load_state().get("notified", {})
