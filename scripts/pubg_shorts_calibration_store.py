#!/usr/bin/env python3
"""Store for PUBG YouTube Shorts owner calibration (index, labels, feed queue)."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
DATA_PUBG = Path(os.environ.get("SHOOTER_PUBG_DATA_ROOT", "/root/data/pubg"))
SHORTS_ROOT = Path(os.environ.get("PUBG_SHORTS_ROOT", "/root/datasets/pubg/youtube_shorts"))
EXEMPLAR_ROOT = Path(
    os.environ.get("HIGHLIGHT_EXEMPLAR_ROOT", str(REPO / "data" / "highlight_exemplars"))
)

INDEX_PATH = Path(os.environ.get("PUBG_SHORTS_INDEX", str(DATA_PUBG / "youtube_shorts_index.json")))
LABELS_PATH = Path(os.environ.get("PUBG_CALIBRATION_LABELS", str(DATA_PUBG / "calibration_labels.json")))
FEED_SENT_PATH = Path(os.environ.get("PUBG_FEED_SENT", str(DATA_PUBG / "calibration_feed_sent.json")))
EVER_DELIVERED_PATH = Path(
    os.environ.get("PUBG_EVER_DELIVERED", str(DATA_PUBG / "calibration_ever_delivered.json"))
)
FEED_PROC_LOCK_PATH = Path(os.environ.get("PUBG_FEED_LOCK", "/tmp/pubg_calibration_feed.lock"))
FEED_SENT_LOCK_PATH = Path(
    os.environ.get("PUBG_FEED_SENT_LOCK", "/tmp/pubg_calibration_feed_sent.lock")
)

from pubg_dislike_reasons import DISLIKE_REASON_CODES, DISLIKE_REASONS  # noqa: F401


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


def id_from_path(path: Path) -> str:
    stem = path.stem
    if stem.startswith("yt_") and len(stem) > 3:
        return stem[3:]
    return stem


def _expected_path(video_id: str) -> Path:
    return SHORTS_ROOT / f"yt_{video_id}.mp4"


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
    path = Path(str(row.get("path", "")))
    if path.name.startswith("yt_"):
        row = {**row, "video_id": id_from_path(path), "id": id_from_path(path)}
    data = load_index()
    candidates: list[dict] = data["candidates"]
    vid = str(row.get("video_id", ""))
    for i, existing in enumerate(candidates):
        if existing.get("video_id") == vid:
            candidates[i] = {**existing, **row}
            save_index(data)
            return
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


def load_feed_sent() -> dict[str, set[str] | dict[str, str]]:
    raw = _read_json(FEED_SENT_PATH, {"sent_ids": [], "sent_file_ids": [], "claimed_ids": {}})
    if not isinstance(raw, dict):
        return {"ids": set(), "file_ids": set(), "claimed": {}}
    claimed_raw = raw.get("claimed_ids", {})
    claimed: dict[str, str] = {}
    if isinstance(claimed_raw, dict):
        claimed = {str(k): str(v) for k, v in claimed_raw.items()}
    return {
        "ids": set(str(x) for x in raw.get("sent_ids", [])),
        "file_ids": set(str(x) for x in raw.get("sent_file_ids", [])),
        "claimed": claimed,
    }


def _write_feed_sent(sent: dict[str, set[str] | dict[str, str]]) -> None:
    claimed = sent.get("claimed", {})
    if not isinstance(claimed, dict):
        claimed = {}
    _write_json(
        FEED_SENT_PATH,
        {
            "sent_ids": sorted(sent["ids"]),
            "sent_file_ids": sorted(sent["file_ids"]),
            "claimed_ids": dict(sorted(claimed.items())),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


def load_ever_delivered() -> set[str]:
    raw = _read_json(EVER_DELIVERED_PATH, {"ids": []})
    if not isinstance(raw, dict):
        return set()
    return {str(x) for x in raw.get("ids", [])}


def mark_ever_delivered(ids: list[str]) -> None:
    if not ids:
        return
    merged = load_ever_delivered() | {str(x) for x in ids if x}
    _write_json(
        EVER_DELIVERED_PATH,
        {"ids": sorted(merged), "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
    )


def mark_feed_sent(ids: list[str], *, paths: list[Path] | None = None) -> None:
    with _feed_sent_lock():
        sent = load_feed_sent()
        sent["ids"].update(str(x) for x in ids if x)
        for path in paths or []:
            if path.name.startswith("yt_"):
                sent["file_ids"].add(id_from_path(path))
                sent["ids"].add(id_from_path(path))
        for vid in ids:
            sent["claimed"].pop(str(vid), None)
        _write_feed_sent(sent)
    mark_ever_delivered([str(x) for x in ids if x])


@contextmanager
def _feed_sent_lock():
    FEED_SENT_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = FEED_SENT_LOCK_PATH.open("w")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def feed_singleton_lock(*, blocking: bool = False):
    FEED_PROC_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = FEED_PROC_LOCK_PATH.open("w")
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def labeled_ids() -> dict[str, str]:
    labels = load_labels()
    out: dict[str, str] = {}

    def add_row(row: dict, label: str) -> None:
        path = Path(str(row.get("path", "")))
        vid = str(row.get("video_id") or row.get("id") or "")
        if vid:
            out[vid] = label
        if path.name.startswith("yt_"):
            out[id_from_path(path)] = label

    for row in labels.get("good", []):
        add_row(row, "good")
    for row in labels.get("bad", []):
        add_row(row, "bad")
    for row in labels.get("feedback", []):
        ol = row.get("owner_label")
        if ol in ("yes", "good"):
            add_row(row, "good")
        elif ol in ("no", "bad"):
            add_row(row, "bad")
    return out


def ingest_sent_blocklist() -> set[str]:
    return load_ever_delivered() | load_feed_sent()["ids"]


def find_candidate(video_id: str) -> dict | None:
    vid = video_id.strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    for row in load_index().get("candidates", []):
        if str(row.get("video_id", "")) == vid:
            return row
    direct = _expected_path(vid)
    if direct.exists():
        return {
            "video_id": vid,
            "path": str(direct),
            "title": vid,
            "url": f"https://www.youtube.com/shorts/{vid}",
            "score": 0.0,
        }
    return None


def mark_feed_blocked(video_id: str, *, reason: str, score: float = 0.0) -> None:
    vid = video_id.strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    upsert_candidate(
        {
            "video_id": vid,
            "id": vid,
            "gameplay_pass": 0,
            "gameplay_score": round(float(score), 4),
            "gameplay_reason": reason,
        }
    )


def claim_feed_candidates(rows: list[dict]) -> list[dict]:
    claimed: list[dict] = []
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _feed_sent_lock():
        sent = load_feed_sent()
        claimed_map: dict[str, str] = sent["claimed"]  # type: ignore[assignment]
        for row in rows:
            vid = str(row.get("video_id", "")).strip()
            path = Path(str(row.get("path", "")))
            if not vid or not path.name.startswith("yt_"):
                continue
            file_id = id_from_path(path)
            if (
                vid in sent["ids"]
                or file_id in sent["ids"]
                or file_id in sent["file_ids"]
                or vid in claimed_map
                or file_id in claimed_map
            ):
                continue
            claimed_map[vid] = now
            claimed_map[file_id] = now
            claimed.append({**row, "video_id": vid, "id": vid, "path": str(path)})
        if claimed:
            _write_feed_sent(sent)
    return claimed


def release_feed_claims(ids: list[str]) -> int:
    released = 0
    with _feed_sent_lock():
        sent = load_feed_sent()
        claimed_map: dict[str, str] = sent["claimed"]  # type: ignore[assignment]
        for raw in ids:
            vid = str(raw).strip()
            if vid.startswith("yt_"):
                vid = vid[3:]
            for key in (vid, f"yt_{vid}"):
                if key in claimed_map:
                    claimed_map.pop(key, None)
                    released += 1
        if released:
            _write_feed_sent(sent)
    return released


def release_stale_claims(*, max_age_sec: float = 600) -> int:
    released = 0
    with _feed_sent_lock():
        sent = load_feed_sent()
        claimed_map: dict[str, str] = sent["claimed"]  # type: ignore[assignment]
        stale: list[str] = []
        for vid, ts in list(claimed_map.items()):
            try:
                age = time.time() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                age = max_age_sec + 1
            if age >= max_age_sec:
                stale.append(vid)
        for vid in stale:
            claimed_map.pop(vid, None)
            released += 1
        if released:
            _write_feed_sent(sent)
    return released


def rebuild_index_from_disk() -> int:
    added = 0
    if not SHORTS_ROOT.exists():
        return 0
    labeled = labeled_ids()
    for mp4 in sorted(SHORTS_ROOT.glob("yt_*.mp4")):
        if mp4.stat().st_size < 8000:
            continue
        vid = id_from_path(mp4)
        if vid in labeled:
            continue
        row = find_candidate(vid) or {}
        upsert_candidate(
            {
                "video_id": vid,
                "id": vid,
                "path": str(mp4),
                "title": row.get("title", vid),
                "url": row.get("url", f"https://www.youtube.com/shorts/{vid}"),
                "score": float(row.get("score") or 0.12),
                "gameplay_pass": int(row.get("gameplay_pass") or 1),
                "gameplay_score": float(row.get("gameplay_score") or 0.55),
                "gameplay_reason": str(row.get("gameplay_reason") or "disk_rebuild"),
                "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        added += 1
    return added


def repair_index() -> int:
    data = load_index()
    rows = data.get("candidates", [])
    best: dict[str, dict] = {}
    for row in rows:
        vid = str(row.get("video_id", "")).strip()
        if not vid or len(vid) != 11:
            continue
        expected = _expected_path(vid)
        if not expected.exists():
            continue
        row = {**row, "path": str(expected)}
        prev = best.get(vid)
        if prev is None or float(row.get("score") or 0) >= float(prev.get("score") or 0):
            best[vid] = row
    old_n = len(rows)
    data["candidates"] = list(best.values())
    save_index(data)
    return old_n - len(data["candidates"])


def _is_excluded(vid: str, path: Path, labeled: dict[str, str], sent: dict) -> bool:
    file_id = id_from_path(path)
    delivered = load_ever_delivered()
    claimed: dict[str, str] = sent.get("claimed", {})  # type: ignore[assignment]
    if vid in labeled or file_id in labeled:
        return True
    if vid in delivered or file_id in delivered:
        return True
    if vid in sent["ids"] or file_id in sent["ids"]:
        return True
    if vid in claimed or file_id in claimed:
        return True
    return False


def _row_passes_pending_gate(row: dict, path: Path) -> bool:
    if int(row.get("gameplay_pass") or 0) != 1:
        return False
    title = str(row.get("title", ""))
    from pubg_shorts_title_gate import pubg_short_title_ok

    if not pubg_short_title_ok(title):
        return False
    try:
        if path.stat().st_size > int(os.environ.get("PUBG_SHORTS_MAX_BYTES", str(50 * 1024 * 1024))):
            return False
    except OSError:
        return False
    return True


def pending_candidates(*, limit: int = 50, repair: bool = True) -> list[dict]:
    if repair:
        repair_index()
    labeled = labeled_ids()
    sent = load_feed_sent()
    out: list[dict] = []
    seen: set[str] = set()
    for row in load_index().get("candidates", []):
        path = _expected_path(str(row.get("video_id", "")))
        if not path.exists():
            continue
        vid = id_from_path(path)
        if _is_excluded(vid, path, labeled, sent) or vid in seen:
            continue
        if not _row_passes_pending_gate(row, path):
            continue
        seen.add(vid)
        out.append({**row, "video_id": vid, "id": vid, "path": str(path)})
    out.sort(key=lambda r: (float(r.get("gameplay_score") or 0), str(r.get("upload_date") or "")), reverse=True)
    return out[:limit]


def copy_exemplar(src: Path, label: str, video_id: str) -> Path | None:
    dest_dir = EXEMPLAR_ROOT / "pubg" / label
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
        "15",
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
    path = Path(row.get("path", ""))
    if not path.exists():
        path = _expected_path(video_id.strip())
    if not path.exists():
        return False, f"file_missing:{video_id}"
    vid = id_from_path(path)

    labels = load_labels()
    entry = {
        "video_id": vid,
        "id": vid,
        "path": str(path),
        "title": row.get("title", ""),
        "url": row.get("url", ""),
        "score": row.get("score", 0),
        "metro_hint": row.get("gameplay_reason", ""),
        "source": "youtube_shorts",
        "reason": reason,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "by_chat": by_chat,
    }
    feedback = {**entry, "owner_label": "yes" if is_good else "no"}

    def _same(row: dict) -> bool:
        p = Path(str(row.get("path", "")))
        return id_from_path(p) == vid if p.name.startswith("yt_") else row.get("video_id") == vid

    labels["feedback"] = [f for f in labels.get("feedback", []) if not _same(f)]
    labels["feedback"].append(feedback)
    if is_good:
        labels["good"] = [g for g in labels.get("good", []) if not _same(g)]
        labels["bad"] = [b for b in labels.get("bad", []) if not _same(b)]
        labels["good"].append(entry)
        exemplar = copy_exemplar(path, "good", vid)
        if exemplar:
            entry["exemplar"] = str(exemplar)
    else:
        labels["bad"] = [b for b in labels.get("bad", []) if not _same(b)]
        labels["good"] = [g for g in labels.get("good", []) if not _same(g)]
        labels["bad"].append(entry)
        exemplar = copy_exemplar(path, "bad", vid)
        if exemplar:
            entry["exemplar"] = str(exemplar)
        mark_feed_blocked(vid, reason=reason or "owner_dislike")

    save_labels(labels)
    from pubg_owner_learning import sync_shorts_label_to_owner_json

    sync_shorts_label_to_owner_json(vid, is_good=is_good, reason=reason)
    try:
        from highlight_scorer import clear_exemplar_cache

        clear_exemplar_cache()
    except ImportError:
        pass
    return True, "good" if is_good else "bad"


def stats() -> dict:
    labels = load_labels()
    feedback = labels.get("feedback", [])
    yes = sum(1 for f in feedback if f.get("owner_label") in ("yes", "good"))
    no = sum(1 for f in feedback if f.get("owner_label") in ("no", "bad"))
    root = EXEMPLAR_ROOT / "pubg"
    good_ex = len(list((root / "good").glob("*.mp4"))) if (root / "good").exists() else 0
    bad_ex = len(list((root / "bad").glob("*.mp4"))) if (root / "bad").exists() else 0
    return {
        "feedback_yes": yes,
        "feedback_no": no,
        "good_exemplars": good_ex,
        "bad_exemplars": bad_ex,
        "delivered": len(load_ever_delivered()),
        "index_total": len(load_index().get("candidates", [])),
        "pending": len(pending_candidates(limit=9999, repair=False)),
    }


def dislike_reason_label(reason: str) -> str:
    from pubg_dislike_reasons import dislike_reason_label as _label

    return _label(reason)


def dislike_reason_keyboard_markup(item_id: str, *, callback_prefix: str = "pubg_short_bad") -> dict:
    from pubg_dislike_reasons import dislike_reason_keyboard_markup as _kb

    return _kb(item_id, callback_prefix=callback_prefix)


def inline_keyboard_markup(video_id: str) -> dict:
    vid = str(video_id).strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    return {
        "inline_keyboard": [
            [
                {"text": "👍 Ок", "callback_data": f"pubg_short_yes:{vid}"},
                {"text": "👎 Не ок", "callback_data": f"pubg_short_no:{vid}"},
            ],
        ]
    }


def labeled_keyboard_markup(label: str, *, reason: str = "", video_id: str = "") -> dict:
    if label == "good":
        vid = str(video_id).strip()
        if vid.startswith("yt_"):
            vid = vid[3:]
        rows: list[list[dict]] = [[{"text": "✅ Ок", "callback_data": "mlbb_noop"}]]
        if vid:
            rows.append([{"text": "📁 HQ", "callback_data": f"pubg_short_hq:{vid}"}])
        return {"inline_keyboard": rows}
    mark = f"❌ {dislike_reason_label(reason)}"
    return {"inline_keyboard": [[{"text": mark, "callback_data": "mlbb_noop"}]]}
