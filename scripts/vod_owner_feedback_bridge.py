#!/usr/bin/env python3
"""Close the owner 👎 loop: ledger + adaptive thresholds + reason-gate memory."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("vod_owner_feedback_bridge")


def apply_owner_feedback(
    game: str,
    *,
    clip_id: str,
    is_good: bool,
    reason: str = "",
    vod_id: str = "",
) -> dict[str, Any]:
    """Record feedback and tighten gates on bad labels. Best-effort; never raises."""
    g = (game or "pubg").strip().lower()
    label = "good" if is_good else "bad"
    out: dict[str, Any] = {"game": g, "clip_id": clip_id, "label": label, "reason": reason}
    try:
        from vod_clip_quality_ledger import record_feedback

        record_feedback(
            g,
            clip_id=str(clip_id),
            label=label,
            reason=reason or label,
            vod_id=str(vod_id or ""),
        )
        out["ledger"] = True
    except Exception as exc:  # noqa: BLE001
        log.warning("ledger feedback failed game=%s clip=%s: %s", g, clip_id, exc)
        out["ledger"] = False

    if is_good:
        return out

    try:
        from game_adaptive_thresholds import note_negative_feedback

        thresholds = note_negative_feedback(g, reason or "")
        out["adaptive"] = thresholds
    except Exception as exc:  # noqa: BLE001
        log.warning("adaptive thresholds failed game=%s: %s", g, exc)
        out["adaptive"] = None
    return out
