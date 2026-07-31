#!/usr/bin/env python3
"""Cross-montage dedup: never reuse the same VOD peak (or whole VOD) across cuts."""

from __future__ import annotations

import os
import time
from pathlib import Path

from vod_state_io import load_json_state, save_json_state

DATA_ROOT = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))


def _registry_path() -> Path:
    return Path(
        os.environ.get(
            "MONTAGE_DEDUP_STATE",
            str(DATA_ROOT / "montage_dedup.json"),
        )
    )


def _default() -> dict:
    return {
        "used_vods": {},  # game -> [vod_id, ...]
        "used_peaks": {},  # "game:vod_id" -> [peak_sec, ...]
        "day_done": {},  # "YYYY-MM-DD" -> {game: {vod_id, montage_id, peaks, at}}
    }


def load_registry() -> dict:
    data = load_json_state(_registry_path(), _default)
    data.setdefault("used_vods", {})
    data.setdefault("used_peaks", {})
    data.setdefault("day_done", {})
    return data


def save_registry(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json_state(_registry_path(), data)


def _peak_key(game: str, vod_id: str) -> str:
    return f"{game.strip().lower()}:{vod_id.strip()}"


def used_vods(game: str) -> set[str]:
    reg = load_registry()
    return {str(x) for x in (reg.get("used_vods") or {}).get(game.strip().lower(), []) if x}


def used_peaks(game: str, vod_id: str) -> list[float]:
    reg = load_registry()
    key = _peak_key(game, vod_id)
    return [float(x) for x in (reg.get("used_peaks") or {}).get(key, [])]


def peak_already_used(
    game: str,
    vod_id: str,
    peak: float,
    *,
    gap_sec: float | None = None,
) -> bool:
    gap = float(
        gap_sec
        if gap_sec is not None
        else os.environ.get("MONTAGE_DEDUP_PEAK_GAP_SEC", "35")
    )
    for prev in used_peaks(game, vod_id):
        if abs(float(peak) - float(prev)) < gap:
            return True
    return False


def filter_rows_exclude_used(
    game: str,
    vod_id: str,
    rows: list[dict],
    *,
    gap_sec: float | None = None,
) -> list[dict]:
    """Drop rows whose peak was already used in a prior montage."""
    out: list[dict] = []
    for row in rows:
        peak = float(row.get("peak_start", row.get("banner_sec", row.get("start") or 0)) or 0)
        if peak_already_used(game, vod_id, peak, gap_sec=gap_sec):
            continue
        out.append(row)
    return out


def prefer_fresh_vods(game: str, vods: list[Path], *, vod_id_fn) -> list[Path]:
    """Unused VODs first; already-montaged VODs only as last resort if allowed."""
    used = used_vods(game)
    allow_reuse = os.environ.get("MONTAGE_ALLOW_VOD_REUSE", "0") == "1"
    fresh = [p for p in vods if vod_id_fn(p) not in used]
    if fresh:
        return fresh
    if allow_reuse:
        return list(vods)
    return []


def day_montage_done(game: str, day: str) -> bool:
    reg = load_registry()
    return bool(((reg.get("day_done") or {}).get(day) or {}).get(game.strip().lower()))


def mark_montage_sent(
    game: str,
    *,
    day: str,
    vod_id: str,
    peaks: list[float],
    montage_id: str = "",
) -> None:
    """Record VOD + peaks so later montages won't repeat them."""
    game = game.strip().lower()
    vod_id = vod_id.strip()
    reg = load_registry()
    vods = reg.setdefault("used_vods", {}).setdefault(game, [])
    if vod_id and vod_id not in vods:
        vods.append(vod_id)
    # Cap history so the file stays small.
    max_vods = int(os.environ.get("MONTAGE_DEDUP_MAX_VODS", "80"))
    if len(vods) > max_vods:
        reg["used_vods"][game] = vods[-max_vods:]
    key = _peak_key(game, vod_id)
    peak_list = reg.setdefault("used_peaks", {}).setdefault(key, [])
    for p in peaks:
        pf = float(p)
        if not any(abs(pf - float(x)) < 2.0 for x in peak_list):
            peak_list.append(pf)
    max_peaks = int(os.environ.get("MONTAGE_DEDUP_MAX_PEAKS", "40"))
    if len(peak_list) > max_peaks:
        reg["used_peaks"][key] = peak_list[-max_peaks:]
    day_map = reg.setdefault("day_done", {}).setdefault(day, {})
    day_map[game] = {
        "vod_id": vod_id,
        "montage_id": montage_id,
        "peaks": [float(x) for x in peaks],
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # Drop old day_done keys (keep ~14 days).
    keep = int(os.environ.get("MONTAGE_DEDUP_KEEP_DAYS", "14"))
    if len(reg["day_done"]) > keep:
        for old in sorted(reg["day_done"].keys())[:-keep]:
            reg["day_done"].pop(old, None)
    save_registry(reg)
