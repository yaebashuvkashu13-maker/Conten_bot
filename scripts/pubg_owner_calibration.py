#!/usr/bin/env python3
"""Owner-labeled PUBG Metro windows for gate tuning and segment ranking."""

from __future__ import annotations

import json
import os
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
    pad_sec: float | None = None,
) -> bool:
    end_sec = start_sec + duration_sec
    for row in labels_for_video(video_path):
        if row.get("label") != label:
            continue
        center = float(row["time_sec"])
        note = str(row.get("note") or row.get("reason") or "")
        pad = float(pad_sec) if pad_sec is not None else owner_bad_pad_sec(note if label == "bad" else "")
        if label == "good" and pad_sec is None:
            pad = float(os.environ.get("PUBG_OWNER_GOOD_PAD_SEC", "10"))
        if start_sec - pad <= center <= end_sec + pad:
            return True
    return False


def owner_bad_pad_sec(note: str = "") -> float:
    """Block radius around owner 👎 — wider for run/empty/loot notes from label bank."""
    base = float(os.environ.get("PUBG_OWNER_BAD_PAD_SEC", "12"))
    text = note.strip().lower()
    if any(token in text for token in ("run", "empty", "loot", "walk", "fly", "no fight", "no_combat")):
        return max(base, float(os.environ.get("PUBG_OWNER_BAD_RUN_PAD_SEC", "30")))
    return base


def has_owner_labels(video_path: Path) -> bool:
    return bool(labels_for_video(video_path))


def apply_owner_send_policy() -> None:
    """Apply owner-label-bank send criteria to the current process.

    Send only clips where the streamer is actually shooting (gun+burst), not run/loot/fly.
    Ground truth: data/pubg_owner_labels.json and runtime Telegram 👍/👎 labels.
    """
    os.environ.setdefault("PUBG_PRESEND_SHOOTING_GATE", "1")
    os.environ.setdefault("PUBG_PRESEND_SCORE_MODE", "1")
    os.environ.setdefault("PUBG_REJECT_LOOT_WALK", "1")
    os.environ.setdefault("PUBG_FAST_RANK_DROP_LOOT_WALK", "1")
    # Singles: owner rates kill/payoff — do not hard-block gunfights missing OCR kill banner.
    os.environ.setdefault("PUBG_EARLY_PAYOFF_REJECT_SINGLES", "0")
    os.environ.setdefault("PUBG_PAYOFF_SCORE_MIN_SINGLES", "0.10")
    os.environ.setdefault("PUBG_QUALITY_SCORE_MIN_SINGLES", "0.28")
    os.environ.setdefault("PUBG_SINGLES_GUN_PAYOFF_BYPASS", "1")
    os.environ.setdefault("PUBG_SINGLES_GUN_QUALITY_BYPASS", "1")
    os.environ.setdefault("PUBG_SINGLE_MIN_GUN_DENSITY", "0.045")
    os.environ.setdefault("PUBG_CLIP_MIN_BURST_RATIO", "4.8")
    os.environ.setdefault("SHOOTER_VOD_MONTAGE_SHOOTING_ONLY", "0")
    os.environ.setdefault("SHOOTER_REQUIRE_AUTHOR_KILL", "0")
    os.environ.setdefault("PUBG_OWNER_BAD_PAD_SEC", "12")
    os.environ.setdefault("PUBG_OWNER_BAD_RUN_PAD_SEC", "30")


def pubg_passes_tiktok_combat_gate(
    video_path: Path,
    start_sec: float,
    gunfire_density: float,
    burst_ratio: float,
    *,
    center_motion: float = 0.0,
) -> tuple[bool, str]:
    """TikTok: visible brawl — anchor to owner-good fights, never talk/sniper-only."""
    anchor_only = os.environ.get("SMART_PUBG_ANCHOR_GOOD_ONLY", "0") == "1"
    owner_near, owner_dist = nearest_owner_label(video_path, start_sec, radius_sec=12.0)

    if has_owner_labels(video_path):
        if owner_near != "good" or owner_dist > 9.0:
            return False, f"tiktok_off_anchor=dist{owner_dist:.0f}"
        min_gun = 0.048 if owner_dist <= 4.0 else 0.056
        min_burst = 4.6 if owner_dist <= 4.0 else 5.6
        if gunfire_density >= min_gun and burst_ratio >= min_burst:
            if gunfire_density < 0.052 and center_motion < 0.030:
                return False, f"tiktok_sniper_only=gun{gunfire_density:.3f}:motion{center_motion:.3f}"
            return True, f"tiktok_anchor_fight=dist{owner_dist:.0f}:gun{gunfire_density:.3f}"
        return False, f"tiktok_anchor_weak=gun{gunfire_density:.3f}:burst{burst_ratio:.2f}"

    if anchor_only:
        return False, "tiktok_no_owner_labels"
    if gunfire_density >= 0.090 and burst_ratio >= 6.0 and center_motion >= 0.035:
        return True, "tiktok_hot_audio"
    return False, f"tiktok_no_brawl=gun{gunfire_density:.3f}:burst{burst_ratio:.2f}"


def pubg_passes_owner_heuristics(
    gunfire_density: float,
    burst_ratio: float,
    audio_rms: float,
    center_motion: float,
    *,
    panns_gun_max: float = 0.0,
) -> tuple[bool, str]:
    """Rules fitted to owner labels on n97cHIR9Qow (2026-06-06)."""
    panns_trust = float(os.environ.get("PUBG_PANNS_TRUST_MIN", "0.35"))
    min_gun_panns = float(os.environ.get("PUBG_PANNS_TRUST_MIN_GUN", "0.040"))
    min_burst_panns = float(os.environ.get("PUBG_PANNS_TRUST_MIN_BURST", "3.0"))
    if panns_gun_max >= panns_trust:
        if gunfire_density >= min_gun_panns and (
            burst_ratio >= min_burst_panns or gunfire_density >= 0.055
        ):
            return True, f"panns_trust={panns_gun_max:.3f}"
        return False, f"panns_no_gun=pan{panns_gun_max:.3f}:gun{gunfire_density:.3f}:burst{burst_ratio:.2f}"
    relax = os.environ.get("PUBG_RELAX_OWNER_HEURISTICS", "0")
    if relax in ("1", "2"):
        if panns_gun_max >= max(0.22, panns_trust - 0.08) and gunfire_density >= 0.020:
            return True, f"panns_audio=pan{panns_gun_max:.3f}:gun{gunfire_density:.3f}"
        if gunfire_density >= 0.040 and burst_ratio >= 3.5:
            return True, f"relax_fight=gun{gunfire_density:.3f}:burst{burst_ratio:.2f}"
        if gunfire_density >= 0.055:
            return True, "relax_fight_audio"
        if relax == "2" and panns_gun_max >= 0.20:
            return True, f"panns_relax_l4={panns_gun_max:.3f}"
    if audio_rms > 0.050 and gunfire_density < 0.015 and panns_gun_max < 0.25:
        return False, f"talk_menu=rms{audio_rms:.4f}:gun{gunfire_density:.3f}"
    if center_motion > 0.22 and gunfire_density < 0.052 and panns_gun_max < 0.30:
        return False, f"run_loot=motion{center_motion:.3f}:gun{gunfire_density:.3f}"
    if center_motion >= 0.075 and gunfire_density < 0.115 and panns_gun_max < 0.28:
        return False, f"run_fake_gun=motion{center_motion:.3f}:gun{gunfire_density:.3f}"
    if gunfire_density < 0.040 and audio_rms > 0.036:
        return False, f"talk_low_gun=rms{audio_rms:.4f}:gun{gunfire_density:.3f}"
    if gunfire_density >= 0.055:
        return True, "fight_audio"
    if center_motion < 0.022 and gunfire_density >= 0.028 and burst_ratio >= 5.5:
        return True, "sniper_hold"
    if gunfire_density >= 0.048 and burst_ratio >= 4.8 and audio_rms < 0.040:
        return True, "light_combat"
    return False, f"below_owner_floor=density{gunfire_density:.3f}:burst{burst_ratio:.2f}"
