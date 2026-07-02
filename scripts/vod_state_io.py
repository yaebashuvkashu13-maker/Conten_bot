#!/usr/bin/env python3
"""Atomic JSON state load/save with .bak recovery (VOD feeds, daily cycle)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("vod_state_io")


def load_json_state(
    path: Path,
    default: dict[str, Any] | Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Load JSON dict; restore from .bak on corrupt file."""
    if callable(default):
        empty = default
    else:
        empty = lambda: dict(default)  # noqa: E731

    if not path.exists():
        return empty()

    backup = path.with_suffix(path.suffix + ".bak")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise json.JSONDecodeError("expected object", "", 0)
        return data
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        log.error("state corrupt %s: %s", path, exc)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        corrupt = path.with_suffix(f"{path.suffix}.corrupt.{stamp}")
        try:
            path.rename(corrupt)
            log.warning("moved corrupt state to %s", corrupt)
        except OSError:
            pass
        if backup.exists():
            try:
                data = json.loads(backup.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    log.warning("restored state from backup %s", backup)
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return empty()


def save_json_state(path: Path, payload: dict[str, Any], *, retries: int = 3) -> None:
    """Atomic write with .bak of last good state (copy before overwrite + seed on first save)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    backup = path.with_suffix(path.suffix + ".bak")
    tmp = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            if path.exists():
                shutil.copy2(path, backup)
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            if not backup.exists():
                shutil.copy2(path, backup)
            return
        except OSError as exc:
            last_exc = exc
            log.warning("state save attempt %s/%s failed: %s", attempt, retries, exc)
            time.sleep(0.2 * attempt)
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"state save failed after {retries} tries: {last_exc}")
