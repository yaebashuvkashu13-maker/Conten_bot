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
    from json_atomic_store import read_json

    return read_json(path, default)


def _write_json(path: Path, payload: dict | list) -> None:
    from json_atomic_store import atomic_write_json

    atomic_write_json(path, payload)


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
    from json_atomic_store import locked_update_json

    sid = str(row.get("segment_id", ""))

    def _upd(data: dict) -> dict:
        rows: list[dict] = data.setdefault("segments", [])
        for i, existing in enumerate(rows):
            if existing.get("segment_id") == sid:
                rows[i] = {**existing, **row}
                break
        else:
            rows.append(row)
        return data

    locked_update_json(_index_path(), {"segments": [], "updated_at": ""}, _upd)


def load_labels() -> dict:
    data = _read_json(_labels_path(), {"good": [], "bad": [], "feedback": []})
    if not isinstance(data, dict):
        return {"good": [], "bad": [], "feedback": []}
    for key in ("good", "bad", "feedback"):
        data.setdefault(key, [])
    return data


def save_labels(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    from json_atomic_store import locked_update_json

    locked_update_json(_labels_path(), {"good": [], "bad": [], "feedback": []}, lambda _: data)


def load_feed_sent() -> set[str]:
    raw = _read_json(_feed_sent_path(), {"sent_ids": []})
    if not isinstance(raw, dict):
        return set()
    return set(str(x) for x in raw.get("sent_ids", []))


def mark_feed_sent(ids: list[str]) -> None:
    from json_atomic_store import locked_update_json

    def _upd(raw: dict) -> dict:
        sent = set(str(x) for x in raw.get("sent_ids", []))
        sent.update(str(x) for x in ids if x)
        return {"sent_ids": sorted(sent), "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    locked_update_json(_feed_sent_path(), {"sent_ids": []}, _upd)


def record_presend_reject(
    segment_id_str: str,
    *,
    reason: str,
    vod: str = "",
    start: float = 0.0,
    score: float = 0.0,
) -> None:
    """Soft-negative from presend gate — feeds owner calibration without sending."""
    labels = load_labels()
    entry = {
        "segment_id": segment_id_str,
        "path": "",
        "vod": vod,
        "start": start,
        "score": score,
        "reason": f"presend:{reason}"[:200],
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "presend_reject",
    }
    labels["bad"] = [b for b in labels.get("bad", []) if b.get("segment_id") != segment_id_str]
    labels["bad"].append(entry)
    feedback = {**entry, "owner_label": "no"}
    labels["feedback"] = [f for f in labels.get("feedback", []) if f.get("segment_id") != segment_id_str]
    labels["feedback"].append(feedback)
    save_labels(labels)


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


def load_owner_labels_json() -> dict:
    data = _read_json(_owner_labels_path(), {"videos": {}})
    if not isinstance(data, dict):
        return {"videos": {}}
    data.setdefault("videos", {})
    return data


def save_owner_labels_json(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(_owner_labels_path(), data)


def append_owner_label_json(
    vod_id: str,
    time_sec: float,
    label: str,
    *,
    note: str = "",
    source: str = "vod_segment",
) -> None:
    """Append hard-negative / gold anchor to mobile_legends_owner_labels.json."""
    vid = vod_id.strip()
    if not vid or label not in ("good", "bad"):
        return
    data = load_owner_labels_json()
    videos: dict = data.setdefault("videos", {})
    rows: list[dict] = list(videos.get(vid, []))
    key = (round(float(time_sec), 1), label)
    seen = {(round(float(r.get("time_sec", 0)), 1), r.get("label")) for r in rows if "time_sec" in r}
    if key in seen:
        return
    entry: dict = {
        "time_sec": round(float(time_sec), 1),
        "label": label,
        "source": source,
    }
    if note:
        entry["note"] = note[:200]
    rows.append(entry)
    videos[vid] = rows
    save_owner_labels_json(data)


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
        try:
            from mlbb_training_archive import archive_vod_segment

            peak = float(row.get("peak_start") or row.get("start") or 0)
            vod_id = str(row.get("vod_id") or row.get("vod") or segment_id_str.rsplit("_", 1)[0])
            archived = archive_vod_segment(path, segment_id_str, vod_id=vod_id, peak_sec=peak)
            if archived:
                entry["training_archive"] = str(archived)
        except ImportError:
            pass
    else:
        labels["bad"] = [b for b in labels.get("bad", []) if b.get("segment_id") != segment_id_str]
        labels["good"] = [g for g in labels.get("good", []) if g.get("segment_id") != segment_id_str]
        labels["bad"].append(entry)
        exemplar = copy_exemplar(path, "bad", segment_id_str)
        if exemplar:
            entry["exemplar"] = str(exemplar)

    try:
        from mlbb_scene_library import register_vod_label

        register_vod_label(
            path=path,
            segment_id=segment_id_str,
            is_good=is_good,
            row=row,
            reason=reason,
            by_chat=by_chat,
            archive_path=str(entry.get("training_archive") or ""),
            exemplar_path=str(entry.get("exemplar") or ""),
        )
    except ImportError:
        pass

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


def good_download_keyboard_markup(segment_id_str: str) -> dict:
    sid = segment_id_str.strip()
    return {
        "inline_keyboard": [
            [{"text": "📥 Скачать оригинал", "callback_data": f"mlbb_hq_vseg:{sid}"}]
        ]
    }


def labeled_keyboard_markup(label: str, *, segment_id: str = "") -> dict:
    if label == "good":
        mark = "✅ Отправлено"
    else:
        mark = "❌ Не ок"
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
