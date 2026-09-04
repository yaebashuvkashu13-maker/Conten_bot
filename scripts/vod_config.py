#!/usr/bin/env python3
"""Typed effective configuration for VOD pipeline — print on startup, fail on conflicts."""

from __future__ import annotations

import json
import os
from typing import Any

from vod_scan_cascade import cascade_limits


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default) == "1"


def _conflicts() -> list[str]:
    """Hard conflicts that must abort startup."""
    issues: list[str] = []
    if _flag("PUBG_REQUIRE_KILL_NOTIFICATION") and os.environ.get("PUBG_KILL_NOTIFICATION_MODE", "prefer") != "required":
        if not _flag("PUBG_KILL_NOTIFICATION_MODE_REQUIRED_OK"):
            issues.append("PUBG_REQUIRE_KILL_NOTIFICATION=1 but PUBG_KILL_NOTIFICATION_MODE!=required")
    if _flag("SHOOTER_VOD_AUDIO_BATCH", "1") and not _flag("SHOOTER_VOD_AUDIO_GENERATOR", "1"):
        if os.environ.get("VOD_GAME", "").lower() == "pubg":
            issues.append("SHOOTER_VOD_AUDIO_BATCH without SHOOTER_VOD_AUDIO_GENERATOR on PUBG")
    if _flag("PUBG_RANKER_AUTO_PROMOTE") and not _flag("PUBG_RANKER_BENCHMARK_REQUIRED", "1"):
        issues.append("PUBG_RANKER_AUTO_PROMOTE without PUBG_RANKER_BENCHMARK_REQUIRED")
    cascade = cascade_limits()
    try:
        clip_top = int(os.environ.get("VOD_CASCADE_CLIP_MAX", str(cascade.clip_visual)) or cascade.clip_visual)
    except ValueError:
        clip_top = cascade.clip_visual
    if clip_top > cascade.panns and cascade.panns > 0:
        issues.append(
            f"VOD_CASCADE_CLIP_MAX={clip_top} exceeds VOD_CASCADE_PANN_MAX={cascade.panns}"
        )
    return issues


def _warnings() -> list[str]:
    """Soft operator warnings — printed, not fatal."""
    warnings: list[str] = []
    try:
        pann_top = int(os.environ.get("SHOOTER_VOD_PANN_TOP_N", "0") or 0)
    except ValueError:
        pann_top = 0
    cascade = cascade_limits()
    if pann_top > 0 and pann_top > cascade.panns:
        warnings.append(
            f"SHOOTER_VOD_PANN_TOP_N={pann_top} exceeds VOD_CASCADE_PANN_MAX={cascade.panns}; "
            "cascade path trims to pann budget before expensive stages"
        )
    labels = os.environ.get("PUBG_OWNER_LABELS_PATH", "")
    if labels.startswith("/root/content_bot_ml/") or labels.startswith("data/"):
        warnings.append(
            f"PUBG_OWNER_LABELS_PATH points at git checkout ({labels}); "
            "prefer /root/data/pubg/pubg_owner_labels.json"
        )
    return warnings


def effective_config() -> dict[str, Any]:
    cascade = cascade_limits()
    return {
        "pubg_only": _flag("VOD_PUBG_ONLY") or _flag("EU_PUBG_ONLY"),
        "montage_max_sec": float(os.environ.get("SHOOTER_VOD_MONTAGE_MAX_SEC", "55")),
        "audio_batch": _flag("SHOOTER_VOD_AUDIO_BATCH", "1"),
        "audio_generator": _flag("SHOOTER_VOD_AUDIO_GENERATOR", "1"),
        "feature_store": _flag("VOD_FEATURE_STORE", "1"),
        "ranked_pool_cache": _flag("VOD_RANKED_POOL_CACHE", "1"),
        "kill_notification_mode": os.environ.get("PUBG_KILL_NOTIFICATION_MODE", "prefer"),
        "quality_score_min": float(os.environ.get("PUBG_QUALITY_SCORE_MIN", "0.48")),
        "fight_score_min": float(os.environ.get("PUBG_FIGHT_SCORE_MIN", "0.42")),
        "payoff_score_min": float(os.environ.get("PUBG_PAYOFF_SCORE_MIN", "0.38")),
        "pann_workers": os.environ.get("SHOOTER_VOD_PANN_WORKERS", ""),
        "cascade": cascade.as_dict(),
        "ranker_model": os.environ.get("PUBG_RANKER_MODEL", "/root/data/pubg/pubg_moment_ranker.joblib"),
        "owner_labels": os.environ.get("PUBG_OWNER_LABELS_PATH", "/root/data/pubg/pubg_owner_labels.json"),
        "conflicts": _conflicts(),
        "warnings": _warnings(),
    }


def config_status() -> dict[str, Any]:
    cfg = effective_config()
    cfg["ok"] = not cfg["conflicts"]
    return cfg


def print_effective_config() -> None:
    print(json.dumps(effective_config(), indent=2, ensure_ascii=False))


def validate_startup() -> None:
    conflicts = _conflicts()
    for warn in _warnings():
        print(f"config warning: {warn}", flush=True)
    if conflicts:
        raise SystemExit(f"config conflict: {'; '.join(conflicts)}")


__all__ = ["config_status", "effective_config", "print_effective_config", "validate_startup"]
