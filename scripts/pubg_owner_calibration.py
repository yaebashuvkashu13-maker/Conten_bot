#!/usr/bin/env python3
"""Owner-labeled PUBG Metro windows for gate tuning and segment ranking."""

from __future__ import annotations

import json
from pathlib import Path

LABELS_PATH = Path("/root/data/mlbb/pubg_owner_labels.json")
REPO_LABELS_PATH = Path(__file__).resolve().parent.parent / "data" / "pubg_owner_labels.json"

# n97cHIR9Qow — owner review 2026-06-06
DEFAULT_LABELS: dict[str, list[dict]] = {
    "n97cHIR9Qow": [
        {"tc": "30:45", "time_sec": 1845.0, "label": "good"},
        {"tc": "33:25", "time_sec": 2005.0, "label": "good"},
        {"tc": "35:05", "time_sec": 2105.0, "label": "bad"},
        {"tc": "35:50", "time_sec": 2150.0, "label": "good"},
        {"tc": "36:55", "time_sec": 2215.0, "label": "bad"},
        {"tc": "38:07", "time_sec": 2287.0, "label": "bad"},
        {"tc": "41:10", "time_sec": 2470.0, "label": "good"},
        {"tc": "42:01", "time_sec": 2521.0, "label": "bad"},
    ],
}


def _read_labels_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if isinstance(payload.get("videos"), dict):
        return payload["videos"]
    return payload if isinstance(payload, dict) else {}


def load_owner_labels() -> dict[str, list[dict]]:
    merged = {vid: list(rows) for vid, rows in DEFAULT_LABELS.items()}
    for path in (LABELS_PATH, REPO_LABELS_PATH):
        for vid, rows in _read_labels_file(path).items():
            merged.setdefault(vid, [])
            seen = {(r.get("time_sec"), r.get("label")) for r in merged[vid]}
            for row in rows:
                key = (row.get("time_sec"), row.get("label"))
                if key not in seen:
                    merged[vid].append(row)
                    seen.add(key)
    return merged


def video_id_from_path(video_path: Path) -> str | None:
    stem = video_path.stem
    if stem.startswith("yt_") and len(stem) > 3:
        return stem[3:]
    return None


def labels_for_video(video_path: Path) -> list[dict]:
    vid = video_id_from_path(video_path)
    if not vid:
        return []
    return load_owner_labels().get(vid, [])


def nearest_owner_label(
    video_path: Path,
    start_sec: float,
    *,
    radius_sec: float = 14.0,
) -> tuple[str | None, float]:
    """Return ('good'|'bad'|None, distance_sec) for the closest owner label."""
    labels = labels_for_video(video_path)
    if not labels:
        return None, 999.0
    best_label: str | None = None
    best_dist = 999.0
    for row in labels:
        dist = abs(float(row["time_sec"]) - start_sec)
        if dist <= radius_sec and dist < best_dist:
            best_dist = dist
            best_label = str(row["label"])
    return best_label, best_dist


def segment_overlaps_owner_label(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    label: str,
    pad_sec: float = 8.0,
) -> bool:
    end_sec = start_sec + duration_sec
    for row in labels_for_video(video_path):
        if row.get("label") != label:
            continue
        center = float(row["time_sec"])
        if start_sec - pad_sec <= center <= end_sec + pad_sec:
            return True
    return False


def pubg_passes_owner_heuristics(
    gunfire_density: float,
    burst_ratio: float,
    audio_rms: float,
    center_motion: float,
) -> tuple[bool, str]:
    """Rules fitted to owner labels on n97cHIR9Qow (2026-06-06)."""
    if audio_rms > 0.050 and gunfire_density < 0.015:
        return False, f"talk_menu=rms{audio_rms:.4f}:gun{gunfire_density:.3f}"
    if center_motion > 0.22 and gunfire_density < 0.052:
        return False, f"run_loot=motion{center_motion:.3f}:gun{gunfire_density:.3f}"
    if gunfire_density < 0.040 and audio_rms > 0.036:
        return False, f"talk_low_gun=rms{audio_rms:.4f}:gun{gunfire_density:.3f}"
    if gunfire_density >= 0.055:
        return True, "fight_audio"
    if center_motion < 0.022 and gunfire_density >= 0.028 and burst_ratio >= 5.5:
        return True, "sniper_hold"
    if gunfire_density >= 0.048 and burst_ratio >= 4.8 and audio_rms < 0.040:
        return True, "light_combat"
    return False, f"below_owner_floor=density{gunfire_density:.3f}:burst{burst_ratio:.2f}"
