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

def _repo_root() -> Path:
    env = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent.parent
    if root.name == "bin" or str(root) == "/usr/local":
        return Path("/root/content_bot_ml")
    return root


REPO = _repo_root()


def _data_mlbb() -> Path:
    return Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))


def _segments_root() -> Path:
    return Path(os.environ.get("MLBB_VOD_SEGMENTS_ROOT", "/root/datasets/mlbb/vod_segments"))


def segments_root() -> Path:
    return _segments_root()


def _index_path() -> Path:
    return Path(os.environ.get("MLBB_VOD_SEGMENT_INDEX", str(_data_mlbb() / "vod_segment_index.json")))


def _labels_path() -> Path:
    return Path(os.environ.get("MLBB_VOD_SEGMENT_LABELS", str(_data_mlbb() / "vod_segment_labels.json")))


def _owner_labels_path() -> Path:
    return Path(
        os.environ.get("MLBB_OWNER_LABELS_PATH", str(_data_mlbb() / "mobile_legends_owner_labels.json"))
    )


def _feed_sent_path() -> Path:
    return Path(os.environ.get("MLBB_VOD_FEED_SENT", str(_data_mlbb() / "vod_segment_feed_sent.json")))


def _exemplar_root() -> Path:
    return Path(
        os.environ.get(
            "HIGHLIGHT_EXEMPLAR_ROOT",
            str(_repo_root() / "data" / "highlight_exemplars"),
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
    data = _read_json(_index_path(), {"segments": [], "updated_at": ""})
    if not isinstance(data, dict):
        return {"segments": [], "updated_at": ""}
    data.setdefault("segments", [])
    return data


def save_index(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(_index_path(), data)


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
    data = _read_json(_labels_path(), {"good": [], "bad": [], "feedback": []})
    if not isinstance(data, dict):
        return {"good": [], "bad": [], "feedback": []}
    for key in ("good", "bad", "feedback"):
        data.setdefault(key, [])
    return data


def save_labels(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(_labels_path(), data)


def load_feed_sent() -> set[str]:
    raw = _read_json(_feed_sent_path(), {"sent_ids": []})
    if not isinstance(raw, dict):
        return set()
    return set(str(x) for x in raw.get("sent_ids", []))


def mark_feed_sent(ids: list[str]) -> None:
    sent = load_feed_sent()
    sent.update(str(x) for x in ids if x)
    _write_json(
        _feed_sent_path(),
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
    direct = _segments_root() / f"seg_{sid}.mp4"
    if direct.exists():
        return {"segment_id": sid, "path": str(direct), "start": 0, "score": 0}
    return None


def _peak_time_sec(row: dict) -> float:
    peak = row.get("peak_start")
    if peak is not None:
        return float(peak)
    return float(row.get("start", 0))


def _vod_id_from_row(row: dict, segment_id_str: str) -> str:
    vod_field = str(row.get("vod", "")).strip()
    if vod_field:
        return vod_youtube_id(Path(vod_field))
    if "_" in segment_id_str:
        return segment_id_str.rsplit("_", 1)[0]
    return segment_id_str[:11]


def append_owner_label_json(
    vod_id: str,
    time_sec: float,
    label: str,
    *,
    note: str = "",
    source: str = "vod_segment",
) -> None:
    """Append hard-negative / gold anchor to mobile_legends_owner_labels.json."""
    from mlbb_owner_learning import append_owner_time_label

    append_owner_time_label(
        vod_id,
        time_sec,
        label,
        note=note,
        source=source,
        scope="segment" if source.startswith("vod") else "",
    )


def load_owner_labels_json() -> dict:
    from mlbb_owner_learning import load_owner_labels_json as _load

    return _load()


def save_owner_labels_json(data: dict) -> None:
    from mlbb_owner_learning import save_owner_labels_json as _save

    _save(data)


def backfill_owner_labels_from_vod_segments() -> int:
    """One-shot sync: all vod_segment_labels → owner_labels.json (dedupe by time+label)."""
    labels = load_labels()
    added = 0
    index_rows = {r.get("segment_id"): r for r in load_index().get("segments", [])}
    for bucket, label in (("good", "good"), ("bad", "bad")):
        for entry in labels.get(bucket, []):
            sid = str(entry.get("segment_id", ""))
            if not sid:
                continue
            idx_row = index_rows.get(sid, {})
            merged = {**idx_row, **entry}
            vid = _vod_id_from_row(merged, sid)
            t_sec = _peak_time_sec(merged)
            before = len(load_owner_labels_json().get("videos", {}).get(vid, []))
            append_owner_label_json(
                vid,
                t_sec,
                label,
                note=str(entry.get("reason") or ""),
                source="vod_segment_backfill",
            )
            after = len(load_owner_labels_json().get("videos", {}).get(vid, []))
            if after > before:
                added += 1
    return added


def copy_exemplar(src: Path, label: str, sid: str) -> Path | None:
    dest_dir = _exemplar_root() / "mobile_legends" / label
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
        path = _segments_root() / f"seg_{segment_id_str}.mp4"
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

    label_name = "good" if is_good else "bad"
    vid = _vod_id_from_row(row, segment_id_str)
    peak_sec = _peak_time_sec(row)
    append_owner_label_json(
        vid,
        peak_sec,
        label_name,
        note=reason,
        source="vod_segment",
    )
    return True, label_name


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


def labeled_keyboard_markup(label: str, *, reason: str = "", segment_id: str = "") -> dict:
    from mlbb_calibration_store import dislike_reason_label

    if label == "good":
        sid = segment_id.strip()
        rows: list[list[dict]] = [[{"text": "✅ Ок", "callback_data": "mlbb_noop"}]]
        if sid:
            rows.append([{"text": "📁 HQ файл", "callback_data": f"mlbb_vseg_hq:{sid}"}])
        return {"inline_keyboard": rows}
    mark = f"❌ {dislike_reason_label(reason)}"
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
