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
    issues: list[str] = []
    if _flag("PUBG_REQUIRE_KILL_NOTIFICATION") and os.environ.get("PUBG_KILL_NOTIFICATION_MODE", "prefer") != "required":
        if not _flag("PUBG_KILL_NOTIFICATION_MODE_REQUIRED_OK"):
            issues.append("PUBG_REQUIRE_KILL_NOTIFICATION=1 but PUBG_KILL_NOTIFICATION_MODE!=required")
    if _flag("SHOOTER_VOD_AUDIO_BATCH", "1") and not _flag("SHOOTER_VOD_AUDIO_GENERATOR", "1"):
        if os.environ.get("VOD_GAME", "").lower() == "pubg":
            issues.append("SHOOTER_VOD_AUDIO_BATCH without SHOOTER_VOD_AUDIO_GENERATOR on PUBG")
    if _flag("PUBG_RANKER_AUTO_PROMOTE") and not _flag("PUBG_RANKER_BENCHMARK_REQUIRED", "1"):
        issues.append("PUBG_RANKER_AUTO_PROMOTE without PUBG_RANKER_BENCHMARK_REQUIRED")
    return issues


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
        "pann_workers": os.environ.get("SHOOTER_VOD_PANN_WORKERS", ""),
        "cascade": cascade.as_dict(),
        "ranker_model": os.environ.get("PUBG_RANKER_MODEL", "/root/data/pubg/pubg_moment_ranker.joblib"),
        "owner_labels": os.environ.get("PUBG_OWNER_LABELS_PATH", "/root/data/pubg/pubg_owner_labels.json"),
        "conflicts": _conflicts(),
    }


def config_status() -> dict[str, Any]:
    cfg = effective_config()
    cfg["ok"] = not cfg["conflicts"]
    return cfg


def print_effective_config() -> None:
    print(json.dumps(effective_config(), indent=2, ensure_ascii=False))


def validate_startup() -> None:
    conflicts = _conflicts()
    if conflicts:
        raise SystemExit(f"config conflict: {'; '.join(conflicts)}")


__all__ = ["config_status", "effective_config", "print_effective_config", "validate_startup"]
