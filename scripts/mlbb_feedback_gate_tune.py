#!/usr/bin/env python3
"""Apply mined owner-feedback patterns to runtime gates and clip ranking."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


# Keys applied at send/collect only — never tighten highlight discovery hook.
SEND_GATE_KEYS = frozenset(
    {
        "MLBB_VOD_MIN_CLIP_SCORE",
        "MLBB_FEEDBACK_MIN_FIGHT_DUR",
        "MLBB_FEEDBACK_REJECT_HOOK_BELOW",
        "MLBB_FEEDBACK_REJECT_FIGHT_DUR_BELOW",
    }
)

# Cap auto-tuned thresholds so feedback mining cannot stall the pipeline.
SEND_GATE_CAPS = {
    "MLBB_VOD_MIN_CLIP_SCORE": 0.16,
    "MLBB_FEEDBACK_REJECT_HOOK_BELOW": 0.10,
    "MLBB_FEEDBACK_REJECT_FIGHT_DUR_BELOW": 26.0,
}


def _enabled() -> bool:
    return os.environ.get("MLBB_FEEDBACK_GATE", "1") == "1"


def _patterns_path() -> Path:
    root = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    return Path(os.environ.get("MLBB_FEEDBACK_PATTERNS_PATH", str(root / "feedback_patterns.json")))


@lru_cache(maxsize=1)
def load_patterns() -> dict[str, Any]:
    path = _patterns_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def clear_patterns_cache() -> None:
    load_patterns.cache_clear()


def apply_feedback_gates(*, force: bool = False) -> dict[str, float]:
    """
  Set os.environ thresholds from feedback_patterns.json.
  Does not override keys already set unless MLBB_FEEDBACK_GATE_FORCE=1 or force=True.
  """
    if not _enabled():
        return {}
    payload = load_patterns()
    gates = payload.get("gates") or {}
    if not gates:
        return {}
    applied: dict[str, float] = {}
    force_apply = force or os.environ.get("MLBB_FEEDBACK_GATE_FORCE", "0") == "1"
    discovery = os.environ.get("MLBB_FEEDBACK_GATE_DISCOVERY", "0") == "1"
    for key, val in gates.items():
        if key == "VIRAL_MLBB_HOOK_MIN" and not discovery:
            continue
        if key not in SEND_GATE_KEYS and not discovery:
            continue
        if key in SEND_GATE_CAPS:
            val = min(float(val), float(SEND_GATE_CAPS[key]))
        if not force_apply and os.environ.get(key):
            continue
        os.environ[key] = str(val)
        applied[key] = float(val)
    return applied


def feedback_reject_row(row: dict) -> tuple[bool, str]:
    """Hard reject candidates that look like frequent 👎 patterns."""
    if not _enabled():
        return False, ""
    if row.get("kill_banner") or row.get("anchor") == "kill_banner":
        return False, ""
    if int(row.get("kill_banner_tier") or 0) >= 2:
        return False, ""
    payload = load_patterns()
    gates = payload.get("gates") or {}
    hook = float(row.get("hook_score") or 0)
    fight_dur = float(row.get("fight_dur") or row.get("duration") or 0)
    hook_floor = float(
        gates.get("MLBB_FEEDBACK_REJECT_HOOK_BELOW")
        or os.environ.get("MLBB_FEEDBACK_REJECT_HOOK_BELOW", "0.08")
    )
    fight_floor = float(
        gates.get("MLBB_FEEDBACK_REJECT_FIGHT_DUR_BELOW")
        or os.environ.get("MLBB_FEEDBACK_REJECT_FIGHT_DUR_BELOW", "28")
    )
    if hook_floor > 0 and hook < hook_floor:
        return True, f"feedback_low_hook:{hook:.3f}<{hook_floor:.3f}"
    if fight_floor > 0 and fight_dur > 0 and fight_dur < fight_floor:
        return True, f"feedback_short_fight:{fight_dur:.1f}<{fight_floor:.1f}"
    return False, ""


def feedback_rank_boost(row: dict) -> float:
    """Positive boost for clips resembling 👍 cluster."""
    if not _enabled():
        return 0.0
    payload = load_patterns()
    prof = payload.get("rank_profile") or {}
    if not prof:
        return 0.0
    hook = float(row.get("hook_score") or 0)
    fight_dur = float(row.get("fight_dur") or row.get("duration") or 0)
    clip = float(row.get("clip_score") or 0)
    hook_t = float(prof.get("hook_target") or 0.27)
    fight_t = float(prof.get("fight_dur_target") or 48.0)
    clip_t = float(prof.get("clip_target") or 0.25)
    hw = float(prof.get("hook_weight") or 0.45)
    fw = float(prof.get("fight_dur_weight") or 0.25)
    cw = float(prof.get("clip_weight") or 0.30)

    hook_s = min(1.0, hook / hook_t) if hook_t > 0 else 0.0
    fight_s = min(1.0, fight_dur / fight_t) if fight_t > 0 and fight_dur > 0 else 0.0
    clip_s = min(1.0, clip / clip_t) if clip_t > 0 else 0.0
    return hw * hook_s + fw * fight_s + cw * clip_s


def feedback_rank_key(row: dict) -> tuple[float, float, float, float]:
    metrics = row.get("highlight_metrics") or {}
    clip_score = float(metrics.get("clip_score") or row.get("clip_score") or 0.0)
    boost = feedback_rank_boost(row)
    return (
        clip_score + boost,
        boost,
        float(row.get("score", 0)),
        float(row.get("hook_score", 0)),
    )
