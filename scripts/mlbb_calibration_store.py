#!/usr/bin/env python3
"""Shared store for MLBB YouTube Shorts calibration (index, labels, exemplars)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
DATA_MLBB = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
SHORTS_ROOT = Path(os.environ.get("MLBB_SHORTS_ROOT", "/root/datasets/mlbb/youtube_shorts"))
EXEMPLAR_ROOT = Path(
    os.environ.get(
        "HIGHLIGHT_EXEMPLAR_ROOT",
        str(REPO / "data" / "highlight_exemplars"),
    )
)

INDEX_PATH = Path(os.environ.get("MLBB_SHORTS_INDEX", str(DATA_MLBB / "youtube_shorts_index.json")))
LABELS_PATH = Path(os.environ.get("MLBB_CALIBRATION_LABELS", str(DATA_MLBB / "calibration_labels.json")))
FEED_SENT_PATH = Path(os.environ.get("MLBB_FEED_SENT", str(DATA_MLBB / "calibration_feed_sent.json")))
REPO_LABELS_PATH = REPO / "data" / "mlbb" / "calibration_labels.json"


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
    if path != REPO_LABELS_PATH and REPO_LABELS_PATH.parent.exists():
        REPO_LABELS_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def load_index() -> dict:
    data = _read_json(INDEX_PATH, {"candidates": [], "updated_at": ""})
    if not isinstance(data, dict):
        return {"candidates": [], "updated_at": ""}
    data.setdefault("candidates", [])
    return data


def save_index(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(INDEX_PATH, data)


def upsert_candidate(row: dict) -> None:
    data = load_index()
    candidates: list[dict] = data["candidates"]
    vid = str(row.get("video_id", ""))
    replaced = False
    for i, existing in enumerate(candidates):
        if existing.get("video_id") == vid:
            candidates[i] = {**existing, **row}
            replaced = True
            break
    if not replaced:
        candidates.append(row)
    save_index(data)


def load_labels() -> dict:
    data = _read_json(LABELS_PATH, {"good": [], "bad": [], "feedback": []})
    if not isinstance(data, dict):
        return {"good": [], "bad": [], "feedback": []}
    for key in ("good", "bad", "feedback"):
        data.setdefault(key, [])
    return data


def save_labels(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(LABELS_PATH, data)


def load_feed_sent() -> set[str]:
    raw = _read_json(FEED_SENT_PATH, {"sent_ids": []})
    if isinstance(raw, dict):
        return set(str(x) for x in raw.get("sent_ids", []))
    return set()


def mark_feed_sent(ids: list[str]) -> None:
    sent = load_feed_sent()
    sent.update(ids)
    _write_json(
        FEED_SENT_PATH,
        {"sent_ids": sorted(sent), "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
    )


def labeled_ids() -> dict[str, str]:
    """video_id -> good|bad"""
    labels = load_labels()
    out: dict[str, str] = {}
    for row in labels.get("good", []):
        vid = str(row.get("video_id") or row.get("id") or "")
        if vid:
            out[vid] = "good"
    for row in labels.get("bad", []):
        vid = str(row.get("video_id") or row.get("id") or "")
        if vid:
            out[vid] = "bad"
    for row in labels.get("feedback", []):
        vid = str(row.get("video_id") or row.get("id") or "")
        label = row.get("owner_label")
        if vid and label in ("yes", "good"):
            out[vid] = "good"
        elif vid and label in ("no", "bad"):
            out[vid] = "bad"
    return out


def find_candidate(video_id: str) -> dict | None:
    vid = video_id.strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    data = load_index()
    for row in data.get("candidates", []):
        if row.get("video_id") == vid or str(row.get("id", "")) == vid:
            return row
        if str(row.get("video_id", "")).startswith(vid):
            return row
    return None


def copy_exemplar(src: Path, label: str, video_id: str) -> Path | None:
    dest_dir = EXEMPLAR_ROOT / "mobile_legends" / label
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"cal_{video_id}.mp4"
    if dest.exists():
        return dest
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-t",
        "12",
        "-c",
        "copy",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False, timeout=120)
    if proc.returncode == 0 and dest.exists():
        return dest
    try:
        shutil.copy2(src, dest)
        return dest
    except OSError:
        return None


def apply_owner_label(
    video_id: str,
    *,
    is_good: bool,
    reason: str = "",
    by_chat: str = "",
) -> tuple[bool, str]:
    row = find_candidate(video_id)
    if not row:
        return False, f"unknown_id:{video_id}"

    vid = str(row.get("video_id", video_id))
    path = Path(row.get("path", ""))
    if not path.exists():
        path = SHORTS_ROOT / f"yt_{vid}.mp4"
    if not path.exists():
        return False, f"file_missing:{vid}"

    labels = load_labels()
    entry = {
        "video_id": vid,
        "id": vid,
        "path": str(path),
        "title": row.get("title", ""),
        "url": row.get("url", ""),
        "score": row.get("score", 0),
        "source": "youtube_shorts",
        "reason": reason,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "by_chat": by_chat,
    }
    feedback = {
        **entry,
        "owner_label": "yes" if is_good else "no",
        "model_score": row.get("score", 0),
    }
    labels["feedback"] = [f for f in labels.get("feedback", []) if f.get("video_id") != vid]
    labels["feedback"].append(feedback)

    if is_good:
        labels["good"] = [g for g in labels.get("good", []) if g.get("video_id") != vid]
        labels["bad"] = [b for b in labels.get("bad", []) if b.get("video_id") != vid]
        labels["good"].append(entry)
        exemplar = copy_exemplar(path, "good", vid)
        if exemplar:
            entry["exemplar"] = str(exemplar)
    else:
        labels["bad"] = [b for b in labels.get("bad", []) if b.get("video_id") != vid]
        labels["good"] = [g for g in labels.get("good", []) if g.get("video_id") != vid]
        labels["bad"].append(entry)
        exemplar = copy_exemplar(path, "bad", vid)
        if exemplar:
            entry["exemplar"] = str(exemplar)

    save_labels(labels)
    return True, "good" if is_good else "bad"


def pending_candidates(*, limit: int = 50) -> list[dict]:
    labeled = labeled_ids()
    sent = load_feed_sent()
    rows = load_index().get("candidates", [])
    out: list[dict] = []
    for row in rows:
        vid = str(row.get("video_id", ""))
        if not vid or vid in labeled or vid in sent:
            continue
        path = Path(row.get("path", ""))
        if not path.exists():
            path = SHORTS_ROOT / f"yt_{vid}.mp4"
        if not path.exists():
            continue
        out.append(row)
    out.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
    return out[:limit]


def stats() -> dict:
    labels = load_labels()
    feedback = labels.get("feedback", [])
    yes = sum(1 for f in feedback if f.get("owner_label") in ("yes", "good"))
    no = sum(1 for f in feedback if f.get("owner_label") in ("no", "bad"))
    agree = 0
    comparable = 0
    for f in feedback:
        model = float(f.get("model_score") or f.get("score") or 0)
        owner_good = f.get("owner_label") in ("yes", "good")
        comparable += 1
        model_positive = model >= 0.35
        if model_positive == owner_good:
            agree += 1
    accuracy = agree / comparable if comparable else 0.0
    good_ex = len(list((EXEMPLAR_ROOT / "mobile_legends" / "good").glob("cal_*.mp4")))
    bad_ex = len(list((EXEMPLAR_ROOT / "mobile_legends" / "bad").glob("cal_*.mp4")))
    return {
        "feedback_yes": yes,
        "feedback_no": no,
        "accuracy": round(accuracy, 4),
        "comparable": comparable,
        "good_labels": len(labels.get("good", [])),
        "bad_labels": len(labels.get("bad", [])),
        "good_exemplars": good_ex,
        "bad_exemplars": bad_ex,
        "index_total": len(load_index().get("candidates", [])),
        "pending": len(pending_candidates(limit=9999)),
    }


def ready_for_eval(*, min_yes: int = 30, min_no: int = 20) -> bool:
    s = stats()
    return s["feedback_yes"] >= min_yes and s["feedback_no"] >= min_no
