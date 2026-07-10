#!/usr/bin/env python3
"""Apply owner banner-calibration labels to live detection gates."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path


def _profile_path() -> Path:
    root = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    return Path(os.environ.get("MLBB_BANNER_CALIB_PROFILE", str(root / "banner_calibration_profile.json")))


def load_profile() -> dict:
    path = _profile_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def gate_enabled() -> bool:
    if os.environ.get("MLBB_BANNER_OWNER_GATE", "1") != "1":
        return False
    prof = load_profile()
    labeled = int(prof.get("labeled", 0))
    min_labels = int(os.environ.get("MLBB_BANNER_OWNER_GATE_MIN_LABELS", "20"))
    return labeled >= min_labels


def savage_strict_enabled() -> bool:
    if os.environ.get("MLBB_BANNER_OWNER_SAVAGE_STRICT", "0") != "1":
        return False
    prof = load_profile()
    pos = prof.get("by_reason") or {}
    return int(pos.get("savage_tier", 0)) + int(pos.get("own_kill_good", 0)) >= 3


def check_banner_frame(frame, *, tier: int = 0) -> tuple[str, str]:
    """
    Owner-calibration decision for HUD patch.
    Returns (decision, reason): pass | reject | neutral.
    """
    if not gate_enabled():
        return "neutral", ""

    try:
        from mlbb_banner_ref_match import (
            match_negative_banner_reference,
            match_positive_owner_reference,
        )
    except ImportError:
        return "neutral", ""

    neg = match_negative_banner_reference(frame)
    if neg is not None:
        score, reason, _path = neg
        return "reject", f"owner_neg:{reason}:{score:.3f}"

    pos = match_positive_owner_reference(frame)
    if pos is not None:
        score, reason, _path = pos
        return "pass", f"owner_pos:{reason}:{score:.3f}"

    if savage_strict_enabled() and tier >= 5:
        return "reject", "owner_pos_required_savage"

    return "neutral", ""


def check_banner_frame_passes(frame, *, tier: int = 0) -> tuple[bool, str]:
    decision, reason = check_banner_frame(frame, tier=tier)
    if decision == "reject":
        return False, reason
    return True, reason or "owner_gate_ok"
