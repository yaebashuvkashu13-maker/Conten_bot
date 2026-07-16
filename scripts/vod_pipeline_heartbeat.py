#!/usr/bin/env python3
"""Small structured heartbeat so watchdogs do not kill healthy long scans."""

from __future__ import annotations

import os
import time
from pathlib import Path

from vod_state_io import save_json_state

_LAST_WRITE = 0.0


def heartbeat(
    stage: str,
    *,
    game: str = "mlbb",
    vod_id: str = "",
    progress: float | None = None,
    candidates_in: int | None = None,
    candidates_out: int | None = None,
    force: bool = False,
) -> None:
    global _LAST_WRITE
    now = time.time()
    interval = max(5.0, float(os.environ.get("VOD_HEARTBEAT_INTERVAL_SEC", "30")))
    if not force and now - _LAST_WRITE < interval:
        return
    root = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    path = Path(os.environ.get("VOD_HEARTBEAT_PATH", str(root / "vod_pipeline_heartbeat.json")))
    payload = {
        "pid": os.getpid(),
        "game": game,
        "vod_id": vod_id,
        "stage": stage,
        "timestamp": now,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if progress is not None:
        payload["progress"] = round(float(progress), 4)
    if candidates_in is not None:
        payload["candidates_in"] = int(candidates_in)
    if candidates_out is not None:
        payload["candidates_out"] = int(candidates_out)
    if game == "mlbb":
        try:
            from mlbb_vod_throughput_mode import heartbeat_extra

            payload.update(heartbeat_extra())
        except Exception:
            pass
    save_json_state(path, payload)
    _LAST_WRITE = now
