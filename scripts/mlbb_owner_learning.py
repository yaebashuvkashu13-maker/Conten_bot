#!/usr/bin/env python3
"""Unified owner feedback for MLBB Shorts + VOD — one anchor store, one training loader."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from highlight_scorer import WINDOW_SEC, normalize_profile

DATA_MLBB = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))


def _shorts_labels_path() -> Path:
    return Path(os.environ.get("MLBB_CALIBRATION_LABELS", str(DATA_MLBB / "calibration_labels.json")))


def _vseg_labels_path() -> Path:
    return Path(os.environ.get("MLBB_VOD_SEGMENT_LABELS", str(DATA_MLBB / "vod_segment_labels.json")))


def _owner_labels_path() -> Path:
    return Path(
        os.environ.get("MLBB_OWNER_LABELS_PATH", str(DATA_MLBB / "mobile_legends_owner_labels.json"))
    )


def _exemplar_root() -> Path:
    return Path(
        os.environ.get(
            "HIGHLIGHT_EXEMPLAR_ROOT",
            str(Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml")) / "data" / "highlight_exemplars"),
        )
    )


SHORTS_SCOPE = "youtube_shorts"
VOD_SCOPE = "vod_segment"


def _read_json(path: Path, default: dict | list) -> dict | list:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_owner_labels_json() -> dict:
    data = _read_json(_owner_labels_path(), {"videos": {}})
    if not isinstance(data, dict):
        return {"videos": {}}
    data.setdefault("videos", {})
    return data


def save_owner_labels_json(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(_owner_labels_path(), data)


def append_owner_time_label(
    video_id: str,
    time_sec: float,
    label: str,
    *,
    note: str = "",
    source: str = "owner",
    scope: str = "",
) -> bool:
    """Canonical time-anchor write used by Shorts and VOD calibration."""
    vid = video_id.strip()
    if not vid or label not in ("good", "bad"):
        return False
    data = load_owner_labels_json()
    videos: dict = data.setdefault("videos", {})
    rows: list[dict] = list(videos.get(vid, []))
    key = (round(float(time_sec), 1), label, source)
    seen = {
        (round(float(r.get("time_sec", 0)), 1), r.get("label"), r.get("source", ""))
        for r in rows
        if "time_sec" in r
    }
    if key in seen:
        return False
    entry: dict = {
        "time_sec": round(float(time_sec), 1),
        "label": label,
        "source": source,
    }
    if scope:
        entry["scope"] = scope
    if note:
        entry["note"] = note[:200]
    rows.append(entry)
    videos[vid] = rows
    save_owner_labels_json(data)
    return True


def sync_shorts_label_to_owner_json(
    video_id: str,
    *,
    is_good: bool,
    reason: str = "",
) -> bool:
    """Mirror Shorts 👍/👎 into mobile_legends_owner_labels.json (time_sec=0, full clip)."""
    return append_owner_time_label(
        video_id,
        0.0,
        "good" if is_good else "bad",
        note=reason,
        source=SHORTS_SCOPE,
        scope="full_clip",
    )


def backfill_shorts_to_owner_labels() -> int:
    """One-shot: calibration_labels.json → owner_labels.json."""
    data = _read_json(_shorts_labels_path(), {"good": [], "bad": []})
    if not isinstance(data, dict):
        return 0
    added = 0
    for bucket, label in (("good", "good"), ("bad", "bad")):
        for row in data.get(bucket, []):
            vid = str(row.get("video_id") or row.get("id") or "").strip()
            if not vid:
                path = Path(str(row.get("path", "")))
                if path.name.startswith("yt_"):
                    vid = path.stem[3:]
            if not vid:
                continue
            if sync_shorts_label_to_owner_json(
                vid,
                is_good=label == "good",
                reason=str(row.get("reason") or ""),
            ):
                added += 1
    return added


def mlbb_classifier_features(metrics) -> list[float]:
    """Shared 6-dim feature vector for MLBB train + inference."""
    return [
        max(0.0, float(metrics.clip_score)),
        float(metrics.minimap_delta),
        float(metrics.skill_delta),
        float(metrics.center_motion),
        float(metrics.hook_score),
        float(getattr(metrics, "visual_dynamics", 0.0) or 0.0),
    ]


def _repo_root() -> Path:
    env = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if env:
        return Path(env)
    return Path("/root/content_bot_ml")


def _resolve_vod(video_id: str) -> Path | None:
    inbox = Path(os.environ.get("HIGHLIGHT_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
    for candidate in (
        inbox / f"yt_{video_id}.mp4",
        _repo_root() / "data" / "samples" / f"yt_{video_id}.mp4",
    ):
        if candidate.exists():
            return candidate
    return None


def load_unified_training_samples(profile: str) -> list[tuple[Path, float, int]]:
    """
    Deduped training samples for mobile_legends from exemplars, Shorts, VOD segments,
    and time anchors — without triple-counting the same owner vote.
    """
    profile = normalize_profile(profile)
    if profile != "mobile_legends":
        return []

    seen: set[tuple] = set()
    out: list[tuple[Path, float, int]] = []

    def add(path: Path, start: float, label: int, key: tuple) -> None:
        if key in seen or not path.exists():
            return
        seen.add(key)
        out.append((path, start, label))

    exemplar_cap = int(os.environ.get("MLBB_TRAIN_MAX_EXEMPLARS", "200"))
    exemplar_dir = _exemplar_root() / "mobile_legends"
    for label_name, cls in (("good", 1), ("bad", 0)):
        folder = exemplar_dir / label_name
        if not folder.exists():
            continue
        for clip in sorted(folder.glob("*.mp4"))[:exemplar_cap]:
            add(clip, 0.5, cls, ("exemplar", clip.name))

    cal_data = _read_json(_shorts_labels_path(), {"good": [], "bad": []})
    if isinstance(cal_data, dict):
        for bucket, cls in (("good", 1), ("bad", 0)):
            for row in cal_data.get(bucket, []):
                vid = str(row.get("video_id") or row.get("id") or "").strip()
                path = Path(str(row.get("path", "")))
                if not vid and path.name.startswith("yt_"):
                    vid = path.stem[3:]
                if vid and ("exemplar", f"cal_{vid}.mp4") in seen:
                    continue
                if path.exists():
                    add(path, 0.15, cls, ("shorts", vid or path.name))

    vseg_data = _read_json(_vseg_labels_path(), {"good": [], "bad": []})
    if isinstance(vseg_data, dict):
        for bucket, cls in (("good", 1), ("bad", 0)):
            for row in vseg_data.get(bucket, []):
                sid = str(row.get("segment_id", ""))
                path = Path(str(row.get("path", "")))
                if sid and ("exemplar", f"vod_{sid}.mp4") in seen:
                    continue
                if path.exists():
                    add(path, 0.5, cls, ("vseg", sid or path.name))

    owner = load_owner_labels_json()
    videos = owner.get("videos", {})
    if isinstance(videos, dict):
        for vid, rows in videos.items():
            if not isinstance(rows, list):
                continue
            vod = _resolve_vod(str(vid))
            if not vod:
                continue
            for row in rows:
                if row.get("source") == SHORTS_SCOPE:
                    continue
                if "time_sec" not in row:
                    continue
                label = 1 if row.get("label") == "good" else 0
                t = float(row["time_sec"])
                start = max(0.0, t - WINDOW_SEC * 0.5)
                add(vod, start, label, ("anchor", vid, round(t, 1), label))

    return out


def owner_labels_for_vod_scan(video_path: Path, profile: str) -> list[dict]:
    """Time anchors for VOD scanning — excludes Shorts full-clip labels on long files."""
    if normalize_profile(profile) != "mobile_legends":
        return []
    from highlight_scorer import _labels_from_vod_segment_store, _video_id_from_path

    try:
        from smart_video_editor import ffprobe_duration

        duration = float(ffprobe_duration(video_path) or 0.0)
    except Exception:
        duration = 0.0
    long_vod = duration > 120.0

    rows: list[dict] = []
    owner_path = _owner_labels_path()
    if owner_path.exists():
        try:
            data = json.loads(owner_path.read_text(encoding="utf-8"))
            vid = _video_id_from_path(video_path)
            for row in data.get("videos", {}).get(vid, []):
                if long_vod and row.get("source") == SHORTS_SCOPE:
                    continue
                rows.append(row)
        except (json.JSONDecodeError, OSError):
            pass
    rows.extend(_labels_from_vod_segment_store(video_path, profile))
    return rows
