#!/usr/bin/env python3
"""Per-game adaptive shooting/motion thresholds learned from owner 👎 on run/menu."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

GAMES = ("pubg", "standoff", "wot")

# Base floors — fight-oriented; never auto-soften below these without explicit env.
BASE: dict[str, dict[str, float]] = {
    "pubg": {
        "gun_density_min": 0.070,
        "burst_ratio_min": 6.5,
        "motion_max_run": 0.18,
        "menu_overlay_max": 0.35,
    },
    "standoff": {
        "gun_density_min": 0.065,
        "burst_ratio_min": 6.0,
        "motion_max_run": 0.20,
        "menu_overlay_max": 0.32,
    },
    "wot": {
        "gun_density_min": 0.040,
        "burst_ratio_min": 3.5,
        "motion_max_run": 0.22,
        "menu_overlay_max": 0.40,
    },
}

RUN_MENU_REASONS = {
    "run",
    "loot_run",
    "running",
    "беготня",
    "explore",  # genshin run/explore
    "menu",
    "меню",
    "menu_lobby",
    "menu_garage",
    "lobby",
    "garage",
}

NO_GUN_REASONS = {"no_gun", "no_shooting", "silent", "нет_стрельбы"}
BAD_RENDER_REASONS = {"bad_render", "render", "blurry", "freeze", "bad_encode"}
BORING_REASONS = {"boring", "скучно", "uninteresting"}


def store_path() -> Path:
    root = Path(os.environ.get("VOD_ADAPTIVE_THRESH_DIR", "/root/data/vod_adaptive_thresholds"))
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        root = Path(os.environ.get("TMPDIR", "/tmp")) / "vod_adaptive_thresholds"
        root.mkdir(parents=True, exist_ok=True)
    return root / "game_thresholds.json"


def _load() -> dict[str, Any]:
    path = store_path()
    if not path.exists():
        return {"games": {}, "updated_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"games": {}, "updated_at": None}
    return data if isinstance(data, dict) else {"games": {}, "updated_at": None}


def _save(data: dict[str, Any]) -> None:
    path = store_path()
    tmp = path.with_suffix(".tmp")
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def thresholds_for(game: str) -> dict[str, float]:
    g = (game or "pubg").strip().lower()
    if g in {"pubg_mobile", "bgmi"}:
        g = "pubg"
    if g in {"standoff2", "standoff_2"}:
        g = "standoff"
    if g not in BASE:
        g = "pubg"
    base = dict(BASE[g])
    data = _load()
    overrides = (data.get("games") or {}).get(g) or {}
    for key, val in overrides.items():
        try:
            base[key] = float(val)
        except (TypeError, ValueError):
            continue
    return base


def _drought_floor_cap(current: float, *env_keys: str) -> float:
    """Under drought soften, never raise floors above VOD_FORCE_* recovery knobs.

    Take the *lowest* valid knob among all keys. Returning on the first hit made a
    stale VOD_FORCE_BURST_RATIO=5.5 block the softer PUBG_CLIP_MIN_BURST_RATIO=3.5
    from .env and kept send-gun rejecting real kill peaks (need>=5.5).
    """
    soften = os.environ.get("VOD_FORCE_SOFTEN", "0") == "1"
    try:
        esc = int(os.environ.get("VOD_FORCE_ESCALATION", "0") or 0)
    except ValueError:
        esc = 0
    if not soften and esc <= 0:
        return current
    floor = float(current)
    saw = False
    for key in env_keys:
        raw = os.environ.get(key)
        if raw is None or raw == "":
            continue
        try:
            floor = min(floor, float(raw)) if saw else min(float(current), float(raw))
            saw = True
        except ValueError:
            continue
    return floor if saw else current


def apply_to_environ(game: str) -> dict[str, float]:
    """Push current thresholds into process env for gate modules.

    Owner 👎 floors tighten quality, but drought recover (VOD_FORCE_*) must still
    be able to soften gun/burst floors — otherwise apply_to_environ overwrites
    force_send soften and drought stays stuck at zero-send.
    """
    t = thresholds_for(game)
    g = (game or "pubg").strip().lower()
    gun = _drought_floor_cap(
        float(t["gun_density_min"]),
        "VOD_FORCE_GUN_DENSITY",
        "PUBG_SINGLE_MIN_GUN_DENSITY",
        "PUBG_CLIP_MIN_GUN_DENSITY",
    )
    burst = _drought_floor_cap(
        float(t["burst_ratio_min"]),
        "VOD_FORCE_BURST_RATIO",
        "PUBG_CLIP_MIN_BURST_RATIO",
    )
    t["gun_density_min"] = gun
    t["burst_ratio_min"] = burst
    os.environ["PUBG_CLIP_MIN_GUN_DENSITY"] = f"{gun:.4f}"
    os.environ["PUBG_SINGLE_MIN_GUN_DENSITY"] = f"{gun:.4f}"
    os.environ["PUBG_CLIP_MIN_BURST_RATIO"] = f"{burst:.2f}"
    os.environ["VISUAL_MENU_OVERLAY_MAX"] = f"{t['menu_overlay_max']:.3f}"
    if g == "pubg":
        os.environ["SMART_PUBG_MIN_GUNFIRE_DENSITY"] = f"{gun:.4f}"
        os.environ["SMART_PUBG_MAX_RUN_MOTION"] = f"{t['motion_max_run']:.3f}"
    elif g == "standoff":
        os.environ["SMART_STANDOFF_MIN_GUNFIRE_DENSITY"] = f"{gun:.4f}"
        os.environ["SMART_STANDOFF_MIN_CENTER_MOTION"] = f"{max(0.008, t['motion_max_run'] * 0.08):.4f}"
    elif g == "wot":
        os.environ["SMART_WOT_MIN_GUNFIRE_DENSITY"] = f"{gun:.4f}"
    return t


def note_negative_feedback(game: str, reason: str = "") -> dict[str, float]:
    """Tighten gates after 👎; different knobs per dislike family."""
    g = (game or "pubg").strip().lower()
    if g not in BASE:
        g = "pubg"
    reason_l = (reason or "").strip().lower()
    tokens = {t for t in reason_l.replace("-", "_").replace(" ", "_").split("_") if t}
    hit_run_menu = bool(tokens & RUN_MENU_REASONS) or any(
        r in reason_l for r in ("loot_run", "menu_lobby", "menu_garage", "беготн", "меню")
    )
    hit_no_gun = bool(tokens & NO_GUN_REASONS) or "no_gun" in reason_l or "стрельб" in reason_l
    hit_render = bool(tokens & BAD_RENDER_REASONS) or "render" in reason_l or "freeze" in reason_l
    hit_boring = bool(tokens & BORING_REASONS) or "boring" in reason_l or "скуч" in reason_l
    data = _load()
    games = dict(data.get("games") or {})
    skip_keys = {"neg_run_menu", "neg_no_gun", "neg_bad_render", "neg_boring"}
    cur = dict(BASE[g])
    cur.update({k: float(v) for k, v in (games.get(g) or {}).items() if k not in skip_keys})
    if not (hit_run_menu or hit_no_gun or hit_render or hit_boring):
        return apply_to_environ(g)
    if hit_run_menu:
        cur["gun_density_min"] = min(0.12, float(cur["gun_density_min"]) + 0.005)
        cur["burst_ratio_min"] = min(10.0, float(cur["burst_ratio_min"]) + 0.15)
        cur["motion_max_run"] = max(0.08, float(cur["motion_max_run"]) - 0.01)
        cur["menu_overlay_max"] = max(0.15, float(cur["menu_overlay_max"]) - 0.02)
        cur["neg_run_menu"] = float((games.get(g) or {}).get("neg_run_menu", 0) + 1)
    if hit_no_gun:
        cur["gun_density_min"] = min(0.13, float(cur["gun_density_min"]) + 0.008)
        cur["burst_ratio_min"] = min(11.0, float(cur["burst_ratio_min"]) + 0.25)
        cur["neg_no_gun"] = float((games.get(g) or {}).get("neg_no_gun", 0) + 1)
    if hit_render:
        cur["menu_overlay_max"] = max(0.12, float(cur["menu_overlay_max"]) - 0.03)
        cur["neg_bad_render"] = float((games.get(g) or {}).get("neg_bad_render", 0) + 1)
    if hit_boring:
        cur["gun_density_min"] = min(0.12, float(cur["gun_density_min"]) + 0.004)
        cur["burst_ratio_min"] = min(10.0, float(cur["burst_ratio_min"]) + 0.10)
        cur["neg_boring"] = float((games.get(g) or {}).get("neg_boring", 0) + 1)
    games[g] = cur
    data["games"] = games
    _save(data)
    return apply_to_environ(g)
