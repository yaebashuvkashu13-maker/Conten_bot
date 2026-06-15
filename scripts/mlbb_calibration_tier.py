#!/usr/bin/env python3
"""Adaptive calibration filter tiers — relax gates when queue stays empty."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

DATA_MLBB = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
STATE_PATH = Path(os.environ.get("MLBB_TIER_STATE_PATH", str(DATA_MLBB / "calibration_tier_state.json")))

# Tier 0 = strict … 3 = starvation (widest net, still blocks junk titles).
TIER_OVERRIDES: dict[int, dict[str, str]] = {
    0: {
        "MLBB_CALIBRATION_TIER": "0",
        "MLBB_CALIBRATION_LENIENT": "0",
        "MLBB_CALIBRATION_MIN_SCORE": "0.12",
        "MLBB_SHORTS_MIN_KILL_SCORE": "0.18",
        "MLBB_SHORTS_REQUIRE_KILL_UI": "1",
        "MLBB_FEED_PRUNE_IDENTITY": "1",
    },
    1: {
        "MLBB_CALIBRATION_TIER": "1",
        "MLBB_CALIBRATION_LENIENT": "1",
        "MLBB_CALIBRATION_MIN_SCORE": "0.05",
        "MLBB_SHORTS_MIN_KILL_SCORE": "0.15",
        "MLBB_SHORTS_REQUIRE_KILL_UI": "1",
        "MLBB_FEED_PRUNE_IDENTITY": "0",
        "MLBB_INGEST_COOLDOWN_SEC": "90",
        "MLBB_FEED_COOLDOWN_PENDING_SEC": "45",
        "MLBB_INGEST_DOWNLOAD_DELAY": "3",
        "MLBB_INGEST_SEARCH_DELAY": "1",
    },
    2: {
        "MLBB_CALIBRATION_TIER": "2",
        "MLBB_CALIBRATION_LENIENT": "1",
        "MLBB_CALIBRATION_MIN_SCORE": "0.03",
        "MLBB_SHORTS_MIN_KILL_SCORE": "0.10",
        "MLBB_SHORTS_REQUIRE_KILL_UI": "1",
        "MLBB_KILL_UI_MIN_SCORE": "0.08",
        "MLBB_KILL_ANNOUNCE_SPIKE_MIN": "0.045",
        "MLBB_ACTIVITY_MIN_MOTION": "0.012",
        "MLBB_ACTIVITY_MIN_HUD_DELTA": "0.0025",
        "MLBB_STREAMER_REQUIRE_MLBB_TITLE": "0",
        "MLBB_INGEST_SKIP_LONG_CLIP_REJECT": "1",
        "MLBB_INGEST_SKIP_LONG_SCAN": "1",
        "MLBB_FEED_PRUNE_IDENTITY": "0",
        "MLBB_INGEST_COOLDOWN_SEC": "60",
        "MLBB_FEED_COOLDOWN_PENDING_SEC": "30",
        "MLBB_FEED_COOLDOWN_SEC": "90",
        "MLBB_INGEST_DOWNLOAD_DELAY": "2",
        "MLBB_INGEST_SEARCH_DELAY": "1",
        "MLBB_INGEST_MAX_DOWNLOADS": "12",
    },
    3: {
        "MLBB_CALIBRATION_TIER": "3",
        "MLBB_CALIBRATION_LENIENT": "1",
        "MLBB_CALIBRATION_MIN_SCORE": "0.02",
        "MLBB_SHORTS_MIN_KILL_SCORE": "0.08",
        "MLBB_SHORTS_REQUIRE_KILL_UI": "0",
        "MLBB_ACTIVITY_MIN_MOTION": "0.012",
        "MLBB_ACTIVITY_MIN_HUD_DELTA": "0.002",
        "MLBB_STREAMER_REQUIRE_MLBB_TITLE": "0",
        "MLBB_INGEST_SKIP_LONG_CLIP_REJECT": "1",
        "MLBB_INGEST_SKIP_LONG_SCAN": "1",
        "MLBB_FEED_PRUNE_IDENTITY": "0",
        "MLBB_INGEST_COOLDOWN_SEC": "45",
        "MLBB_FEED_COOLDOWN_SEC": "60",
        "MLBB_FEED_COOLDOWN_PENDING_SEC": "20",
        "MLBB_VOD_PAUSE_WHEN_SHORTS_PENDING": "0",
        "MLBB_INGEST_MAX_DOWNLOADS": "15",
        "MLBB_SHORTS_INGEST_DAYS": "730",
        "MLBB_SHORTS_MIN_POOL": "4",
    },
}


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_tier(*, pending: int, state: dict | None = None, now: float | None = None) -> int:
    """Pick tier from queue size and how long we've had nothing to send."""
    state = dict(state or _load_state())
    now = float(now or time.time())
    low_pending = int(os.environ.get("MLBB_TIER_PENDING_LOW", "3"))
    healthy = int(os.environ.get("MLBB_TIER_PENDING_HEALTHY", "15"))
    lenient_band = int(os.environ.get("MLBB_TIER_PENDING_LENIENT", "5"))
    step_sec = float(os.environ.get("MLBB_TIER_STEP_SEC", "240"))  # ~4 min
    starve_sec = float(os.environ.get("MLBB_TIER_STARVE_SEC", "300"))  # ~5 min

    empty_since = float(state.get("empty_since") or 0.0)
    if pending >= healthy:
        return 0
    if pending >= lenient_band:
        return 1

    if pending < low_pending:
        if empty_since <= 0:
            empty_since = now
    else:
        empty_since = 0.0

    empty_for = now - empty_since if empty_since > 0 else 0.0
    if pending == 0:
        # Empty queue — start relaxed immediately, escalate further with time.
        if empty_for >= starve_sec:
            tier = 3
        elif empty_for >= step_sec:
            tier = 2
        else:
            tier = 2
    elif empty_for >= starve_sec:
        tier = 3
    elif empty_for >= step_sec:
        tier = 2
    else:
        tier = 1

    prev = int(state.get("current_tier", tier))
    state["empty_since"] = empty_since or None
    state["current_tier"] = tier
    if tier != prev:
        state["last_tier_change"] = now
    _save_state(state)
    return tier


def tier_env(tier: int) -> dict[str, str]:
    tier = max(0, min(3, int(tier)))
    return dict(TIER_OVERRIDES.get(tier, TIER_OVERRIDES[1]))


def apply_tier(base: dict[str, str], *, pending: int) -> tuple[int, dict[str, str]]:
    tier = resolve_tier(pending=pending)
    env = dict(base)
    env.update(tier_env(tier))
    env["MLBB_CALIBRATION_TIER"] = str(tier)
    return tier, env


def note_ingest_saved(*, count: int = 1) -> None:
    """Record successful ingest — tier follows queue size, not single saves."""
    if count <= 0:
        return
    state = _load_state()
    state["last_save_at"] = time.time()
    _save_state(state)


def current_tier() -> int:
    return int(_load_state().get("current_tier", 1))


def kill_ui_required() -> bool:
    return os.environ.get("MLBB_SHORTS_REQUIRE_KILL_UI", "1") == "1"


def prune_identity_enabled() -> bool:
    return os.environ.get("MLBB_FEED_PRUNE_IDENTITY", "1") == "1"


def skip_long_clip_reject() -> bool:
    return os.environ.get("MLBB_INGEST_SKIP_LONG_CLIP_REJECT", "0") == "1"
