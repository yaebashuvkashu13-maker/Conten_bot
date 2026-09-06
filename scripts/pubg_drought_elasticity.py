#!/usr/bin/env python3
"""PUBG drought elasticity: −15%/idle-hour on numeric floors, +10% after a send.

Only scales numeric combat/payoff thresholds. Hard rejects (menu / loot / bot-farm /
owner_bad) stay untouched. Floor 70% of baseline, ceiling 110%.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# Keys we are allowed to ease/tighten. Lower value => softer (except where noted).
ELASTIC_KEYS: tuple[str, ...] = (
    "PUBG_PAYOFF_SCORE_MIN_SINGLES",
    "PUBG_QUALITY_SCORE_MIN_SINGLES",
    "PUBG_SINGLE_MIN_GUN_DENSITY",
    "PUBG_CLIP_MIN_GUN_DENSITY",
    "PUBG_PRESEND_MIN_GUN_DENSITY",
    "PUBG_POOL_MIN_GUN_DENSITY",
    "SMART_PUBG_MIN_GUNFIRE_DENSITY",
    "SHOOTER_VOD_DENSE_GUN_MIN",
    "PUBG_CLIP_MIN_BURST_RATIO",
    "PUBG_AUTHOR_KILL_PANNS_FLASH_MIN",
    "PUBG_DROUGHT_PANNS_OVERRIDE",
    "HIGHLIGHT_PANN_GUN_MIN",
    "PUBG_COMBAT_PANN_MIN",
)

# Higher value => softer for these (override floors / style blend).
ELASTIC_INVERT_KEYS: frozenset[str] = frozenset(
    {
        "PUBG_DROUGHT_PANNS_OVERRIDE",  # lower PANNs floor = softer; scale DOWN with idle
    }
)

# Baseline = owner-calibrated Metro fight-act floors (global, all VODs).
# Old 0.045 gun / 0.16 payoff rejected real OCR-blind sprays (6mWLqNBX1pE).
DEFAULT_BASELINE: dict[str, float] = {
    # Owner-calibrated Metro fight-act floors (global for all VODs).
    "PUBG_PAYOFF_SCORE_MIN_SINGLES": 0.10,
    "PUBG_QUALITY_SCORE_MIN_SINGLES": 0.28,
    "PUBG_SINGLE_MIN_GUN_DENSITY": 0.032,
    "PUBG_CLIP_MIN_GUN_DENSITY": 0.032,
    "PUBG_PRESEND_MIN_GUN_DENSITY": 0.032,
    "PUBG_POOL_MIN_GUN_DENSITY": 0.032,
    "SMART_PUBG_MIN_GUNFIRE_DENSITY": 0.038,
    "SHOOTER_VOD_DENSE_GUN_MIN": 0.032,
    "PUBG_CLIP_MIN_BURST_RATIO": 4.5,
    "PUBG_AUTHOR_KILL_PANNS_FLASH_MIN": 0.42,
    "PUBG_DROUGHT_PANNS_OVERRIDE": 0.38,
    "HIGHLIGHT_PANN_GUN_MIN": 0.18,
    "PUBG_COMBAT_PANN_MIN": 0.18,
}

HARD_LOCK: dict[str, str] = {
    "PUBG_HARD_REJECT_MENU_OVERLAY": "1",
    "PUBG_REJECT_LOOT_WALK": "1",
    "PUBG_REJECT_BOT_FARM": "1",
    "PUBG_QUALITY_BOT_FARM_GATE": "1",
    "PUBG_RELAX_OWNER_HEURISTICS": "0",
}


def _state_path() -> Path:
    root = Path(
        os.environ.get(
            "PUBG_DROUGHT_ELASTICITY_PATH",
            "/root/data/pubg/drought_elasticity.json",
        )
    )
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        root = Path(os.environ.get("TMPDIR", "/tmp")) / "pubg_drought_elasticity.json"
    return root


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {"baseline": dict(DEFAULT_BASELINE), "last_sent_ts": 0.0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"baseline": dict(DEFAULT_BASELINE), "last_sent_ts": 0.0}
    if not isinstance(data, dict):
        return {"baseline": dict(DEFAULT_BASELINE), "last_sent_ts": 0.0}
    baseline = dict(DEFAULT_BASELINE)
    raw_base = data.get("baseline") or {}
    if isinstance(raw_base, dict):
        for key, val in raw_base.items():
            try:
                baseline[str(key)] = float(val)
            except (TypeError, ValueError):
                continue
    try:
        last_sent = float(data.get("last_sent_ts") or 0.0)
    except (TypeError, ValueError):
        last_sent = 0.0
    return {"baseline": baseline, "last_sent_ts": last_sent}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    payload = {
        "baseline": state.get("baseline") or dict(DEFAULT_BASELINE),
        "last_sent_ts": float(state.get("last_sent_ts") or 0.0),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def idle_hours(*, now: float | None = None, last_sent_ts: float | None = None) -> float:
    if last_sent_ts is None:
        last_sent_ts = float(_load_state().get("last_sent_ts") or 0.0)
    if last_sent_ts <= 0:
        # No send recorded — treat as long drought so elasticity can engage.
        try:
            return max(0.0, float(os.environ.get("PUBG_DROUGHT_ELASTICITY_BOOT_IDLE_HOURS", "3")))
        except ValueError:
            return 3.0
    now_ts = time.time() if now is None else float(now)
    return max(0.0, (now_ts - float(last_sent_ts)) / 3600.0)


def elasticity_scale(*, hours_idle: float | None = None) -> float:
    """Return multiplier for numeric thresholds.

    Idle: −15% per hour (linear), floor 0.70.
    First hour after a send: +10% harden (1.10).
    """
    if os.environ.get("PUBG_DROUGHT_ELASTICITY", "1") != "1":
        return 1.0
    try:
        rate = float(os.environ.get("PUBG_DROUGHT_ELASTICITY_IDLE_RATE", "0.15"))
    except ValueError:
        rate = 0.15
    try:
        floor = float(os.environ.get("PUBG_DROUGHT_ELASTICITY_FLOOR", "0.70"))
    except ValueError:
        floor = 0.70
    try:
        ceil = float(os.environ.get("PUBG_DROUGHT_ELASTICITY_CEIL", "1.10"))
    except ValueError:
        ceil = 1.10
    hours = idle_hours() if hours_idle is None else max(0.0, float(hours_idle))
    if hours < 1.0:
        # Harden right after a successful send.
        return min(ceil, float(os.environ.get("PUBG_DROUGHT_ELASTICITY_POST_SEND", "1.10")))
    scale = 1.0 - rate * hours
    return max(floor, min(ceil, scale))


def note_successful_send(*, ts: float | None = None) -> dict[str, Any]:
    """Record a shipped clip so the next hour hardens +10% off baseline."""
    state = _load_state()
    state["last_sent_ts"] = float(ts if ts is not None else time.time())
    _save_state(state)
    return state


def apply_elasticity_to_environ(*, hours_idle: float | None = None) -> dict[str, Any]:
    """Scale elastic numeric env keys; re-assert hard locks. Returns debug info."""
    if os.environ.get("PUBG_DROUGHT_ELASTICITY", "1") != "1":
        return {"enabled": False, "scale": 1.0}
    state = _load_state()
    baseline = dict(DEFAULT_BASELINE)
    baseline.update({k: float(v) for k, v in (state.get("baseline") or {}).items()})
    # Capture current env as baseline for keys never seen before.
    for key in ELASTIC_KEYS:
        if key not in baseline:
            raw = os.environ.get(key)
            if raw not in (None, ""):
                try:
                    baseline[key] = float(raw)
                except ValueError:
                    pass
    state["baseline"] = baseline
    hours = idle_hours(last_sent_ts=float(state.get("last_sent_ts") or 0.0)) if hours_idle is None else float(hours_idle)
    scale = elasticity_scale(hours_idle=hours)
    applied: dict[str, float] = {}
    for key in ELASTIC_KEYS:
        base = float(baseline.get(key, DEFAULT_BASELINE.get(key, 0.0)))
        if base <= 0:
            continue
        value = base * scale
        os.environ[key] = f"{value:.4f}"
        applied[key] = value
    # Hard locks — never softened by elasticity.
    if os.environ.get("VOD_FORCE_SOFTEN", "0") != "1":
        for key, val in HARD_LOCK.items():
            if key == "PUBG_RELAX_OWNER_HEURISTICS" and os.environ.get("VOD_FORCE_RELAX_OWNER", "0") == "1":
                continue
            os.environ[key] = val
    else:
        # Even under force-soften, keep menu/loot/bot hard.
        os.environ["PUBG_HARD_REJECT_MENU_OVERLAY"] = "1"
        os.environ["PUBG_REJECT_LOOT_WALK"] = os.environ.get("VOD_FORCE_REJECT_LOOT", "1")
        os.environ["PUBG_REJECT_BOT_FARM"] = "1"
        os.environ["PUBG_QUALITY_BOT_FARM_GATE"] = "1"
    active = scale < 0.999
    os.environ["PUBG_DROUGHT_ELASTICITY_ACTIVE"] = "1" if active else "0"
    os.environ["PUBG_DROUGHT_ELASTICITY_SCALE"] = f"{scale:.4f}"
    os.environ["PUBG_DROUGHT_ELASTICITY_IDLE_HOURS"] = f"{hours:.3f}"
    _save_state(state)
    return {"enabled": True, "scale": scale, "hours_idle": hours, "applied": applied}


__all__ = [
    "ELASTIC_KEYS",
    "apply_elasticity_to_environ",
    "elasticity_scale",
    "idle_hours",
    "note_successful_send",
]
