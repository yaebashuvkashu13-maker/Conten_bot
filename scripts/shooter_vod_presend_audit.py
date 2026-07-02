#!/usr/bin/env python3
"""Presend audit: verify rendered clip is real combat before Telegram send."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("shooter_presend_audit")


def _require_rendered_check() -> bool:
    return os.environ.get("SHOOTER_VOD_PRESEND_AUDIT", "1") == "1"


def audit_pubg_segment(
    rendered: Path,
    *,
    source_vod: Path | None = None,
    source_start: float = 0.0,
    profile: str = "pubg",
) -> tuple[bool, str, dict[str, Any]]:
    """
    Multimodal presend check on the actual file the user will receive.

    Industry pattern (killfeed OCR + gunfire bursts + HUD motion):
    - strict combat gate on rendered mp4 (not just source VOD window)
    - killfeed OCR bonus / weak-signal rescue
    - reject audio-only PANNs trust without visual gunfire
    """
    from mlbb_vod_segment_feed import _ffprobe_duration
    from pubg_combat_gate import pubg_passes_combat_gate
    from pubg_killfeed_ocr import score_killfeed_segment
    from pubg_shooting_gate import pubg_probe_segment

    dur = float(_ffprobe_duration(rendered) or 0.0)
    if dur < 1.0:
        return False, "rendered_too_short", {"duration": dur}

    report: dict[str, Any] = {"duration": round(dur, 2), "rendered": str(rendered)}

    ok, reason, combat_row = pubg_passes_combat_gate(
        rendered, 0.0, dur, profile, scan_fast=False
    )
    report["combat"] = {"ok": ok, "reason": reason, **combat_row}

    probe = pubg_probe_segment(rendered, 0.0, dur)
    report["audio"] = {
        "gunfire_density": probe.get("gunfire_density"),
        "burst_ratio": probe.get("burst_ratio"),
        "center_motion": probe.get("center_motion"),
        "panns_gun_max": combat_row.get("panns_gun_max"),
    }

    kf_density, kf_row = score_killfeed_segment(rendered, 0.0, dur, profile)
    report["killfeed"] = kf_row

    min_gun = float(os.environ.get("PUBG_PRESEND_MIN_GUN_DENSITY", "0.040"))
    min_burst = float(os.environ.get("PUBG_PRESEND_MIN_BURST", "3.5"))
    min_kf = float(os.environ.get("PUBG_KILLFEED_PRESEND_MIN", "0.20"))
    gun = float(probe.get("gunfire_density") or 0.0)
    burst = float(probe.get("burst_ratio") or 0.0)
    panns = float(combat_row.get("panns_gun_max") or 0.0)

    has_gun_audio = gun >= min_gun and burst >= min_burst
    has_killfeed = kf_density >= min_kf
    has_strong_panns = panns >= float(os.environ.get("PUBG_PANNS_PRESEND_MIN", "0.42"))

    report["signals"] = {
        "gun_audio": has_gun_audio,
        "killfeed": has_killfeed,
        "strong_panns": has_strong_panns,
        "combat_gate": ok,
    }

    if ok and (has_gun_audio or has_killfeed or (has_strong_panns and gun >= min_gun * 0.6)):
        log.info(
            "presend audit PASS %s gun=%.3f burst=%.1f kf=%.2f panns=%.3f reason=%s",
            rendered.name,
            gun,
            burst,
            kf_density,
            panns,
            reason,
        )
        return True, reason, report

    if not ok:
        log.warning("presend audit REJECT %s combat=%s", rendered.name, reason)
        return False, f"combat_gate:{reason}", report

    log.warning(
        "presend audit REJECT %s weak_signals gun=%.3f burst=%.1f kf=%.2f panns=%.3f",
        rendered.name,
        gun,
        burst,
        kf_density,
        panns,
    )
    return False, f"weak_combat:gun{gun:.3f}:burst{burst:.1f}:kf{kf_density:.2f}", report


def audit_shooter_presend(
    game: str,
    rendered: Path,
    *,
    source_vod: Path | None = None,
    source_start: float = 0.0,
    profile: str = "",
) -> tuple[bool, str, dict[str, Any]]:
    if not _require_rendered_check():
        return True, "audit_disabled", {}
    if game == "pubg" or profile in ("pubg", "standoff"):
        return audit_pubg_segment(
            rendered,
            source_vod=source_vod,
            source_start=source_start,
            profile=profile or "pubg",
        )
    return True, "audit_skip_game", {"game": game}
