#!/usr/bin/env python3
"""Champion/challenger ranker deployment — never auto-promote without benchmark."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

DEFAULT_MODELS_DIR = "/root/data/pubg/ranker_models"
CHAMPION_NAME = "champion.joblib"
CHAMPION_META = "champion.json"


def models_dir() -> Path:
    return Path(os.environ.get("PUBG_RANKER_MODELS_DIR", DEFAULT_MODELS_DIR))


def champion_path() -> Path:
    override = os.environ.get("PUBG_RANKER_CHAMPION_PATH", "").strip()
    if override:
        return Path(override)
    return models_dir() / CHAMPION_NAME


def challenger_path() -> Path:
    return models_dir() / "challenger.joblib"


def keep_last_n() -> int:
    return max(3, min(8, int(os.environ.get("PUBG_RANKER_KEEP_MODELS", "5"))))


def _meta_path(model_file: Path) -> Path:
    return model_file.with_suffix(".json")


def load_champion_meta() -> dict[str, Any]:
    meta_file = models_dir() / CHAMPION_META
    if not meta_file.is_file():
        path = champion_path()
        sidecar = _meta_path(path)
        if sidecar.is_file():
            meta_file = sidecar
        else:
            return {}
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def register_candidate(source: Path, *, tag: str = "") -> Path:
    """Archive a trained model as timestamped candidate; returns archive path."""
    models_dir().mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    safe_tag = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tag)[:40]
    name = f"candidate_{stamp}"
    if safe_tag:
        name += f"_{safe_tag}"
    archive = models_dir() / f"{name}.joblib"
    shutil.copy2(source, archive)
    sidecar = _meta_path(source)
    if sidecar.is_file():
        shutil.copy2(sidecar, _meta_path(archive))
    _prune_old_models()
    return archive


def _prune_old_models() -> None:
    candidates = sorted(
        models_dir().glob("candidate_*.joblib"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in candidates[keep_last_n() :]:
        old.unlink(missing_ok=True)
        _meta_path(old).unlink(missing_ok=True)


def compare_benchmark(
    champion_report: dict[str, Any],
    challenger_report: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Return (promote_ok, reasons). Challenger must not regress key metrics."""
    reasons: list[str] = []
    champ_bad = float(champion_report.get("bad_accepted_hits") or 0)
    chall_bad = float(challenger_report.get("bad_accepted_hits") or 0)
    if chall_bad > champ_bad:
        reasons.append(f"bad_accept_regressed {champ_bad}->{chall_bad}")
    champ_recall = float(champion_report.get("good_accepted_rate") or 0)
    chall_recall = float(challenger_report.get("good_accepted_rate") or 0)
    if chall_recall + 1e-6 < champ_recall:
        reasons.append(f"accepted_recall_regressed {champ_recall:.3f}->{chall_recall:.3f}")
    champ_speed = float(champion_report.get("approved_clips_per_min") or 0)
    chall_speed = float(challenger_report.get("approved_clips_per_min") or 0)
    if champ_speed > 0 and chall_speed < champ_speed * 0.95:
        reasons.append(f"speed_regressed {champ_speed:.2f}->{chall_speed:.2f}")
    return len(reasons) == 0, reasons


def promote_challenger(
    challenger: Path,
    *,
    benchmark_report: dict[str, Any] | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    """Promote challenger to champion after benchmark gate (unless force)."""
    if not challenger.is_file():
        return False, "challenger_missing"
    if not force and benchmark_report is not None:
        champ_meta = load_champion_meta()
        ok, reasons = compare_benchmark(champ_meta, benchmark_report)
        if not ok:
            return False, ";".join(reasons)
    models_dir().mkdir(parents=True, exist_ok=True)
    champ = champion_path()
    if champ.is_file():
        backup = models_dir() / f"rollback_{time.strftime('%Y%m%dT%H%M%S')}.joblib"
        shutil.copy2(champ, backup)
        sidecar = _meta_path(champ)
        if sidecar.is_file():
            shutil.copy2(sidecar, _meta_path(backup))
    shutil.copy2(challenger, champ)
    sidecar = _meta_path(challenger)
    if sidecar.is_file():
        shutil.copy2(sidecar, models_dir() / CHAMPION_META)
    elif benchmark_report:
        (models_dir() / CHAMPION_META).write_text(
            json.dumps(benchmark_report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    os.environ["PUBG_RANKER_MODEL"] = str(champ)
    return True, "promoted"


def rollback_champion() -> tuple[bool, str]:
    """Restore most recent rollback_* snapshot."""
    rollbacks = sorted(
        models_dir().glob("rollback_*.joblib"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not rollbacks:
        return False, "no_rollback_snapshot"
    latest = rollbacks[0]
    champ = champion_path()
    shutil.copy2(latest, champ)
    sidecar = _meta_path(latest)
    if sidecar.is_file():
        shutil.copy2(sidecar, models_dir() / CHAMPION_META)
    return True, f"rolled_back_from={latest.name}"


__all__ = [
    "champion_path",
    "compare_benchmark",
    "load_champion_meta",
    "promote_challenger",
    "register_candidate",
    "rollback_champion",
]
