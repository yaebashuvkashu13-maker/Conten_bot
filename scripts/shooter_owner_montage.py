#!/usr/bin/env python3
"""Owner-good fight anchors → shooter склейка without full-VOD rediscover.

Mirrors the manual recovery path: seed montage parts from owner 👍 / labeled
good fight times, soft-allow those windows at montage presend, prefer such VODs.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("shooter_owner_montage")

# Owner-confirmed brawl windows (not sniper-only). Same set used in pubg_brawl_direct.
PUBG_BRAWL_ANCHORS_BY_VOD: dict[str, list[float]] = {
    "n97cHIR9Qow": [1845.0, 2150.0, 2470.0],
}
# Sniper / hold windows labeled good but unsuitable for combat склейка.
PUBG_SNIPER_SKIP: frozenset[float] = frozenset({2005.0})


def owner_anchor_montage_enabled() -> bool:
    return os.environ.get("SHOOTER_VOD_OWNER_ANCHOR_MONTAGE", "1") == "1"


def _video_id(vod: Path) -> str:
    stem = vod.stem
    if stem.startswith("yt_") and len(stem) > 3:
        return stem[3:]
    return stem


def _peaks_from_pubg_calibration(vod: Path) -> list[float]:
    try:
        from pubg_owner_calibration import labels_for_video
    except ImportError:
        return []
    out: list[float] = []
    for row in labels_for_video(vod):
        if str(row.get("label") or "") != "good":
            continue
        try:
            t = float(row["time_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if any(abs(t - s) <= 2.0 for s in PUBG_SNIPER_SKIP):
            continue
        out.append(t)
    return out


def _peaks_from_highlight_labels(vod: Path, profile: str) -> list[float]:
    try:
        from highlight_scorer import _owner_anchor_starts
    except ImportError:
        return []
    try:
        return [float(t) for t in _owner_anchor_starts(vod, profile)]
    except Exception as exc:  # noqa: BLE001 — best-effort seed
        log.debug("highlight owner anchors failed: %s", exc)
        return []


def _peaks_from_feedback_labels(game: str, vod: Path) -> list[float]:
    """👍 feedback on previously sent segments of this VOD."""
    from shooter_vod_segment_store import _paths

    path = _paths(game)["labels"]
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    vid = _video_id(vod)
    out: list[float] = []
    for row in data.get("good", []) + [
        r for r in data.get("feedback", []) if r.get("owner_label") in ("yes", "good")
    ]:
        sid = str(row.get("segment_id") or "")
        vod_field = str(row.get("vod") or "")
        row_vid = ""
        if vod_field:
            vp = Path(vod_field)
            row_vid = vp.stem[3:] if vp.stem.startswith("yt_") else vp.stem
        elif sid.startswith(f"{vid}_"):
            row_vid = vid
        if row_vid != vid:
            continue
        peak = row.get("peak_start", row.get("start"))
        if peak is None and sid.startswith(f"{vid}_"):
            tail = sid[len(vid) + 1 :]
            if tail.startswith("m") and "_" in tail:
                # composite montage id — expand part peaks
                try:
                    out.extend(float(x) for x in tail[1:].split("_") if x.replace(".", "", 1).isdigit())
                except ValueError:
                    pass
                continue
            try:
                peak = float(tail.rsplit("_", 1)[-1])
            except ValueError:
                continue
        try:
            out.append(float(peak))
        except (TypeError, ValueError):
            continue
    return out


def owner_good_fight_peaks(game: str, vod: Path) -> list[float]:
    """Deduped owner-good fight times for montage seeding."""
    if not owner_anchor_montage_enabled():
        return []
    profile = {
        "pubg": "pubg",
        "standoff": "standoff",
        "wot": "wot",
        "genshin": "genshin",
    }.get(game, game)
    peaks: list[float] = []
    vid = _video_id(vod)
    if game == "pubg":
        peaks.extend(PUBG_BRAWL_ANCHORS_BY_VOD.get(vid, []))
        peaks.extend(_peaks_from_pubg_calibration(vod))
    peaks.extend(_peaks_from_highlight_labels(vod, profile))
    peaks.extend(_peaks_from_feedback_labels(game, vod))
    # Dedup within 8s, keep chronological.
    peaks.sort()
    deduped: list[float] = []
    for t in peaks:
        if t < 45.0:
            continue
        if game == "pubg" and any(abs(t - s) <= 2.0 for s in PUBG_SNIPER_SKIP):
            continue
        if any(abs(t - p) <= 8.0 for p in deduped):
            continue
        deduped.append(float(t))
    return deduped


def vod_has_owner_montage_anchors(game: str, vod: Path, *, min_clips: int = 3) -> bool:
    return len(owner_good_fight_peaks(game, vod)) >= min_clips


def owner_good_pool(
    game: str,
    vod: Path,
    *,
    lead_sec: float = 6.0,
    part_sec: float = 18.0,
) -> list[dict]:
    """Minimal highlight-style pool rows from owner-good peaks (no rediscover)."""
    peaks = owner_good_fight_peaks(game, vod)
    if not peaks:
        return []
    pool: list[dict] = []
    for peak in peaks:
        start = max(0.0, float(peak) - lead_sec)
        pool.append(
            {
                "start": float(peak),
                "peak_start": float(peak),
                "score": 0.85,
                "input_duration": part_sec,
                "output_duration": part_sec,
                "highlight_metrics": {"clip_score": 0.85, "owner_anchor": True},
                "owner_anchor": True,
                "gate_reason": "owner_good_anchor",
            }
        )
    log.info(
        "owner-anchor pool game=%s vod=%s peaks=%s",
        game,
        vod.name,
        [int(p) for p in peaks],
    )
    return pool


def peak_near_owner_good(
    game: str,
    vod: Path,
    peak_sec: float,
    *,
    radius_sec: float | None = None,
) -> bool:
    radius = float(
        radius_sec
        if radius_sec is not None
        else os.environ.get("SHOOTER_VOD_OWNER_ANCHOR_RADIUS_SEC", "18")
    )
    for t in owner_good_fight_peaks(game, vod):
        if abs(float(peak_sec) - t) <= radius:
            return True
    return False


def soft_allow_owner_montage_part(
    game: str,
    vod: Path,
    peak_sec: float,
    gate_ok: bool,
    gate_reason: str,
) -> tuple[bool, str]:
    """If owner labeled this fight good, ship the montage part even when gate is noisy."""
    if not owner_anchor_montage_enabled():
        return gate_ok, gate_reason
    if os.environ.get("SHOOTER_VOD_OWNER_ANCHOR_SOFT_ALLOW", "1") != "1":
        return gate_ok, gate_reason
    if not peak_near_owner_good(game, vod, peak_sec):
        return gate_ok, gate_reason
    if gate_ok:
        return True, f"owner_good+{gate_reason}"
    # Still block hard owner-bad / metro mismatches upstream; gate noise is OK.
    hard = ("owner_bad_window", "metro_", "not_metro")
    if any(str(gate_reason).startswith(h) for h in hard):
        return False, gate_reason
    return True, f"owner_good_soft={gate_reason}"
