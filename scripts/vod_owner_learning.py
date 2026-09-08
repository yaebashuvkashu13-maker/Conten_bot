#!/usr/bin/env python3
"""Owner 👍/👎 for VOD segments — exemplars + time anchors (all VOD games)."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from highlight_scorer import WINDOW_SEC, _video_id_from_path, normalize_profile

PROFILE_TO_GAME: dict[str, str] = {
    "pubg": "pubg",
    "standoff": "standoff",
    "genshin": "genshin",
    "wot": "wot",
    "mobile_legends": "mlbb",
}

_OWNER_JSON_NAMES: dict[str, str] = {
    "pubg": "pubg_owner_labels.json",
    "standoff": "standoff_owner_labels.json",
    "genshin": "genshin_owner_labels.json",
    "wot": "wot_owner_labels.json",
    "mobile_legends": "mobile_legends_owner_labels.json",
}

_OWNER_ENV_KEYS: dict[str, str] = {
    "pubg": "PUBG_OWNER_LABELS_PATH",
    "standoff": "STANDOFF_OWNER_LABELS_PATH",
    "genshin": "GENSHIN_OWNER_LABELS_PATH",
    "wot": "WOT_OWNER_LABELS_PATH",
    "mobile_legends": "MLBB_OWNER_LABELS_PATH",
}


def _repo_root() -> Path:
    return Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))


def exemplar_root() -> Path:
    return Path(
        os.environ.get("HIGHLIGHT_EXEMPLAR_ROOT", str(_repo_root() / "data" / "highlight_exemplars"))
    )


def owner_labels_path(profile: str, *, create: bool = False) -> Path | None:
    """Runtime labels path — git data/ is seed only (see runtime_labels.py)."""
    if os.environ.get("VOD_RUNTIME_LABELS", "1") == "1":
        from runtime_labels import ensure_runtime_labels, runtime_labels_path as runtime_path

        path = runtime_path(profile, create=create)
        if path is not None:
            if create or path.exists():
                return path
            return ensure_runtime_labels(profile)
    p = normalize_profile(profile)
    if p not in _OWNER_JSON_NAMES:
        return None
    env_key = _OWNER_ENV_KEYS[p]
    default = _repo_root() / "data" / _OWNER_JSON_NAMES[p]
    path = Path(os.environ.get(env_key, str(default)))
    if create or path.exists():
        return path
    fallback = Path(f"/root/data/mlbb/{_OWNER_JSON_NAMES[p]}")
    return fallback if fallback.exists() else path


def vod_segment_labels_path(profile: str) -> Path | None:
    p = normalize_profile(profile)
    if p == "mobile_legends":
        return Path(
            os.environ.get(
                "MLBB_VOD_SEGMENT_LABELS",
                str(Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb")) / "vod_segment_labels.json"),
            )
        )
    game = PROFILE_TO_GAME.get(p)
    if not game:
        return None
    from shooter_vod_segment_store import _paths

    return _paths(game)["labels"]


def peak_time_sec(row: dict, segment_id_str: str = "") -> float:
    if row.get("peak_start") is not None:
        return float(row["peak_start"])
    if row.get("start") is not None:
        return float(row["start"])
    sid = segment_id_str or str(row.get("segment_id") or "")
    if "_" in sid:
        try:
            return float(sid.rsplit("_", 1)[-1])
        except ValueError:
            pass
    return 0.0


def vod_id_from_row(row: dict, segment_id_str: str = "") -> str:
    vod_field = str(row.get("vod_id") or row.get("vod") or "")
    if vod_field:
        p = Path(vod_field)
        if p.name.startswith("yt_"):
            return p.stem[3:][:11]
        return p.stem[:11]
    sid = segment_id_str or str(row.get("segment_id") or "")
    if "_" in sid:
        return sid.rsplit("_", 1)[0][:11]
    return ""


def load_owner_labels(profile: str) -> dict:
    path = owner_labels_path(profile, create=True)
    if path is None:
        return {"videos": {}}
    if not path.exists():
        return {"videos": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"videos": {}}
    if not isinstance(data, dict):
        return {"videos": {}}
    data.setdefault("videos", {})
    return data


def save_owner_labels(profile: str, data: dict) -> None:
    if os.environ.get("VOD_RUNTIME_LABELS", "1") == "1":
        from runtime_labels import save_runtime_labels

        save_runtime_labels(profile, data)
        return
    path = owner_labels_path(profile, create=True)
    if path is None:
        return
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def append_owner_time_label(
    profile: str,
    video_id: str,
    time_sec: float,
    label: str,
    *,
    note: str = "",
    source: str = "vod_segment",
) -> bool:
    vid = video_id.strip()
    if not vid or label not in ("good", "bad", "uncertain"):
        return False
    data = load_owner_labels(profile)
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
    if note:
        entry["note"] = note[:200]
    rows.append(entry)
    videos[vid] = rows
    save_owner_labels(profile, data)
    return True


def copy_vod_exemplar(profile: str, src: Path, label: str, segment_id_str: str) -> Path | None:
    p = normalize_profile(profile)
    dest_dir = exemplar_root() / p / label
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"vod_{segment_id_str}.mp4"
    try:
        shutil.copy2(src, dest)
        return dest
    except OSError:
        return None


def clear_learning_cache() -> None:
    from highlight_scorer import clear_exemplar_cache

    clear_exemplar_cache()


def labels_from_vod_segment_store(video_path: Path, profile: str) -> list[dict]:
    """👍/👎 on sent VOD clips — block/rescore zones on next scan."""
    path = vod_segment_labels_path(profile)
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    vid = _video_id_from_path(video_path)
    out: list[dict] = []
    for bucket, label in (("good", "good"), ("bad", "bad")):
        for row in data.get(bucket, []):
            sid = str(row.get("segment_id", ""))
            row_vid = vod_id_from_row(row, sid)
            if row_vid != vid:
                continue
            time_sec = peak_time_sec(row, sid)
            out.append(
                {
                    "time_sec": time_sec,
                    "label": label,
                    "source": "vod_segment_labels",
                    "note": str(row.get("reason") or "")[:120],
                }
            )
    return out


def owner_labels_for_vod_scan(video_path: Path, profile: str) -> list[dict]:
    """Merge static owner JSON + Telegram VOD segment labels."""
    p = normalize_profile(profile)
    rows: list[dict] = []
    opath = owner_labels_path(p, create=False)
    if opath and opath.exists():
        try:
            data = json.loads(opath.read_text(encoding="utf-8"))
            vid = _video_id_from_path(video_path)
            rows.extend(list(data.get("videos", {}).get(vid, [])))
        except (json.JSONDecodeError, OSError):
            pass
    rows.extend(labels_from_vod_segment_store(video_path, p))
    return rows


def backfill_owner_labels_from_vod_segments(profile: str) -> int:
    """Sync vod_segment_labels good/bad → owner_labels.json."""
    path = vod_segment_labels_path(profile)
    if path is None or not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    added = 0
    for bucket, label in (("good", "good"), ("bad", "bad")):
        for entry in data.get(bucket, []):
            sid = str(entry.get("segment_id", ""))
            if not sid:
                continue
            vid = vod_id_from_row(entry, sid)
            if not vid:
                continue
            if append_owner_time_label(
                profile,
                vid,
                peak_time_sec(entry, sid),
                label,
                note=str(entry.get("reason") or ""),
                source="vod_segment_backfill",
            ):
                added += 1
    return added


def exemplar_counts(profile: str) -> tuple[int, int]:
    root = exemplar_root() / normalize_profile(profile)
    good = len(list((root / "good").glob("*.mp4"))) if (root / "good").is_dir() else 0
    bad = len(list((root / "bad").glob("*.mp4"))) if (root / "bad").is_dir() else 0
    return good, bad
