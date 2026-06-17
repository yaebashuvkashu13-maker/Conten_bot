#!/usr/bin/env python3
"""Atomic JSON read/write helpers for parallel worker + telegram bot."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def read_json(path: Path, default: T) -> T:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def atomic_write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def locked_update_json(
    path: Path,
    default: T,
    updater: Callable[[T], T],
    *,
    lock_timeout_sec: float = 30.0,
) -> T:
    """Read-modify-write under an exclusive flock on path.lock."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            data = read_json(path, default)
            updated = updater(data)
            atomic_write_json(path, updated)  # type: ignore[arg-type]
            return updated
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
