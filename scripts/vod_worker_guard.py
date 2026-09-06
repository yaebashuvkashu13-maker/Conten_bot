#!/usr/bin/env python3
"""Worker heartbeat, per-VOD time/memory budgets, and corrupt-source quarantine."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _root() -> Path:
    p = Path(os.environ.get("VOD_WORKER_GUARD_DIR", "/root/data/vod_worker_guard"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def heartbeat_path(worker: str = "pubg_feed") -> Path:
    return _root() / f"{worker}.heartbeat"


def quarantine_path() -> Path:
    return _root() / "quarantine.json"


def write_heartbeat(worker: str = "pubg_feed", **extra: Any) -> None:
    payload = {"ts": time.time(), "worker": worker, **extra}
    path = heartbeat_path(worker)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def heartbeat_age_sec(worker: str = "pubg_feed") -> float | None:
    path = heartbeat_path(worker)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return max(0.0, time.time() - float(data.get("ts") or 0.0))
    except Exception:
        return None


def heartbeat_stale(worker: str = "pubg_feed", *, max_age_sec: float | None = None) -> bool:
    age = heartbeat_age_sec(worker)
    if age is None:
        return True
    limit = float(max_age_sec if max_age_sec is not None else os.environ.get("VOD_HEARTBEAT_MAX_AGE_SEC", "900"))
    return age > limit


def _load_quarantine() -> dict[str, Any]:
    path = quarantine_path()
    if not path.exists():
        return {"items": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"items": {}}
    except Exception:
        return {"items": {}}


def _save_quarantine(data: dict[str, Any]) -> None:
    path = quarantine_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def is_quarantined(vod_key: str) -> bool:
    items = _load_quarantine().get("items") or {}
    row = items.get(str(vod_key))
    if not isinstance(row, dict):
        return False
    until = float(row.get("until") or 0.0)
    return until > time.time()


def note_vod_failure(vod_key: str, reason: str = "timeout") -> dict[str, Any]:
    """After N failures, quarantine source for cooldown hours."""
    threshold = int(os.environ.get("VOD_QUARANTINE_FAILS", "3"))
    hours = float(os.environ.get("VOD_QUARANTINE_HOURS", "12"))
    data = _load_quarantine()
    items = dict(data.get("items") or {})
    row = dict(items.get(str(vod_key)) or {})
    fails = int(row.get("fails") or 0) + 1
    row.update({"fails": fails, "reason": reason, "last_ts": time.time()})
    if fails >= threshold:
        row["until"] = time.time() + hours * 3600.0
    items[str(vod_key)] = row
    data["items"] = items
    _save_quarantine(data)
    return row


def clear_vod_failures(vod_key: str) -> None:
    data = _load_quarantine()
    items = dict(data.get("items") or {})
    if str(vod_key) in items:
        items.pop(str(vod_key), None)
        data["items"] = items
        _save_quarantine(data)


@dataclass
class VodBudget:
    max_sec: float
    max_rss_mb: float
    started: float

    @classmethod
    def start(cls) -> "VodBudget":
        return cls(
            max_sec=float(os.environ.get("VOD_WORKER_MAX_SEC", "1200")),
            max_rss_mb=float(os.environ.get("VOD_WORKER_MAX_RSS_MB", "6000")),
            started=time.time(),
        )

    def expired(self) -> bool:
        return (time.time() - self.started) >= self.max_sec

    def rss_mb(self) -> float:
        try:
            # Linux VmRSS in kB
            for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
        except Exception:
            return 0.0
        return 0.0

    def over_memory(self) -> bool:
        rss = self.rss_mb()
        return rss > 0 and rss >= self.max_rss_mb

    def check(self) -> tuple[bool, str]:
        if self.expired():
            return False, "vod_time_budget"
        if self.over_memory():
            return False, f"vod_rss_budget:{self.rss_mb():.0f}MB"
        return True, "ok"
