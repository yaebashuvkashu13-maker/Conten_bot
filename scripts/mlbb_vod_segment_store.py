#!/usr/bin/env python3
"""Store for MLBB VOD segment calibration (individual clips, owner 👍/👎)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
DATA_MLBB = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
SEGMENTS_ROOT = Path(os.environ.get("MLBB_VOD_SEGMENTS_ROOT", "/root/datasets/mlbb/vod_segments"))
INDEX_PATH = Path(os.environ.get("MLBB_VOD_SEGMENT_INDEX", str(DATA_MLBB / "vod_segment_index.json")))
LABELS_PATH = Path(os.environ.get("MLBB_VOD_SEGMENT_LABELS", str(DATA_MLBB / "vod_segment_labels.json")))
FEED_SENT_PATH = Path(os.environ.get("MLBB_VOD_FEED_SENT", str(DATA_MLBB / "vod_segment_feed_sent.json")))
EXEMPLAR_ROOT = Path(
    os.environ.get(
        "HIGHLIGHT_EXEMPLAR_ROOT",
        str(REPO / "data" / "highlight_exemplars"),
    )
)


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


def vod_youtube_id(path: Path) -> str:
    stem = path.stem
    if stem.startswith("yt_") and len(stem) >= 14:
        return stem[3:14]
    match = re.search(r"(?:^|_)([A-Za-z0-9_-]{11})$", stem)
    if match:
        return match.group(1)
    match = re.search(r"([A-Za-z0-9_-]{11})", stem)
    return match.group(1) if match else stem[:24]


def segment_id(vod_path: Path, start: float) -> str:
    return f"{vod_youtube_id(vod_path)}_{int(round(start))}"


def load_index() -> dict:
    data = _read_json(INDEX_PATH, {"segments": [], "updated_at": ""})
    if not isinstance(data, dict):
        return {"segments": [], "updated_at": ""}
    data.setdefault("segments", [])
    return data


def save_index(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(INDEX_PATH, data)


def upsert_segment(row: dict) -> None:
    data = load_index()
    rows: list[dict] = data["segments"]
    sid = str(row.get("segment_id", ""))
    replaced = False
    for i, existing in enumerate(rows):
        if existing.get("segment_id") == sid:
            rows[i] = {**existing, **row}
            replaced = True
            break
    if not replaced:
        rows.append(row)
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
    if not isinstance(raw, dict):
        return set()
    return set(str(x) for x in raw.get("sent_ids", []))


def mark_feed_sent(ids: list[str]) -> None:
    sent = load_feed_sent()
    sent.update(str(x) for x in ids if x)
    _write_json(
        FEED_SENT_PATH,
        {"sent_ids": sorted(sent), "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
    )


def labeled_ids() -> dict[str, str]:
    labels = load_labels()
    out: dict[str, str] = {}
    for row in labels.get("good", []):
        sid = str(row.get("segment_id", ""))
        if sid:
            out[sid] = "good"
    for row in labels.get("bad", []):
        sid = str(row.get("segment_id", ""))
        if sid:
            out[sid] = "bad"
    for row in labels.get("feedback", []):
        sid = str(row.get("segment_id", ""))
        label = row.get("owner_label")
        if sid and label in ("yes", "good"):
            out[sid] = "good"
        elif sid and label in ("no", "bad"):
            out[sid] = "bad"
    return out


def find_segment(segment_id_str: str) -> dict | None:
    sid = segment_id_str.strip()
    for row in load_index().get("segments", []):
        if row.get("segment_id") == sid:
            return row
    direct = SEGMENTS_ROOT / f"seg_{sid}.mp4"
    if direct.exists():
        return {"segment_id": sid, "path": str(direct), "start": 0, "score": 0}
    return None


def copy_exemplar(src: Path, label: str, sid: str) -> Path | None:
    dest_dir = EXEMPLAR_ROOT / "mobile_legends" / label
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"vod_{sid}.mp4"
    if dest.exists():
        return dest
    try:
        shutil.copy2(src, dest)
        return dest
    except OSError:
        return None


def apply_owner_label(
    segment_id_str: str,
    *,
    is_good: bool,
    reason: str = "",
    by_chat: str = "",
) -> tuple[bool, str]:
    row = find_segment(segment_id_str)
    if not row:
        return False, f"unknown_segment:{segment_id_str}"
    path = Path(row.get("path", ""))
    if not path.exists():
        path = SEGMENTS_ROOT / f"seg_{segment_id_str}.mp4"
    if not path.exists():
        return False, f"file_missing:{segment_id_str}"

    labels = load_labels()
    entry = {
        "segment_id": segment_id_str,
        "path": str(path),
        "vod": row.get("vod", ""),
        "start": row.get("start", 0),
        "score": row.get("score", 0),
        "hook_score": row.get("hook_score", 0),
        "reason": reason,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "by_chat": by_chat,
        "source": "vod_segment",
    }
    feedback = {**entry, "owner_label": "yes" if is_good else "no"}

    labels["feedback"] = [f for f in labels.get("feedback", []) if f.get("segment_id") != segment_id_str]
    labels["feedback"].append(feedback)

    if is_good:
        labels["good"] = [g for g in labels.get("good", []) if g.get("segment_id") != segment_id_str]
        labels["bad"] = [b for b in labels.get("bad", []) if b.get("segment_id") != segment_id_str]
        labels["good"].append(entry)
        exemplar = copy_exemplar(path, "good", segment_id_str)
        if exemplar:
            entry["exemplar"] = str(exemplar)
    else:
        labels["bad"] = [b for b in labels.get("bad", []) if b.get("segment_id") != segment_id_str]
        labels["good"] = [g for g in labels.get("good", []) if g.get("segment_id") != segment_id_str]
        labels["bad"].append(entry)
        exemplar = copy_exemplar(path, "bad", segment_id_str)
        if exemplar:
            entry["exemplar"] = str(exemplar)

    save_labels(labels)
    return True, "good" if is_good else "bad"


def inline_keyboard_markup(segment_id_str: str) -> dict:
    sid = segment_id_str.strip()
    return {
        "inline_keyboard": [
            [
                {"text": "👍 Ок", "callback_data": f"mlbb_vseg_yes:{sid}"},
                {"text": "👎 Не ок", "callback_data": f"mlbb_vseg_no:{sid}"},
            ]
        ]
    }


def labeled_keyboard_markup(label: str) -> dict:
    mark = "✅ Ок" if label == "good" else "❌ Не ок"
    return {"inline_keyboard": [[{"text": mark, "callback_data": "mlbb_noop"}]]}


def stats() -> dict:
    labels = load_labels()
    feedback = labels.get("feedback", [])
    yes = sum(1 for f in feedback if f.get("owner_label") in ("yes", "good"))
    no = sum(1 for f in feedback if f.get("owner_label") in ("no", "bad"))
    sent = load_feed_sent()
    labeled = labeled_ids()
    pending = sum(
        1
        for row in load_index().get("segments", [])
        if row.get("segment_id") not in labeled and row.get("segment_id") not in sent
    )
    return {
        "feedback_yes": yes,
        "feedback_no": no,
        "index_total": len(load_index().get("segments", [])),
        "pending": pending,
        "good_labels": len(labels.get("good", [])),
        "bad_labels": len(labels.get("bad", [])),
    }
