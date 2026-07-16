#!/usr/bin/env python3
"""Shared store for MLBB YouTube Shorts calibration (index, labels, exemplars)."""

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
EVER_DELIVERED_PATH = Path(
    os.environ.get("MLBB_EVER_DELIVERED", str(DATA_MLBB / "calibration_ever_delivered.json"))
)
FEED_PROC_LOCK_PATH = Path(os.environ.get("MLBB_FEED_LOCK", "/tmp/mlbb_calibration_feed.lock"))
FEED_SENT_LOCK_PATH = Path(
    os.environ.get("MLBB_FEED_SENT_LOCK", "/tmp/mlbb_calibration_feed_sent.lock")
)
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
    path = Path(str(row.get("path", "")))
    if path.name.startswith("yt_"):
        row = {**row, "video_id": id_from_path(path), "id": id_from_path(path)}
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


def id_from_path(path: Path) -> str:
    """Canonical YouTube id from yt_{id}.mp4 on disk."""
    stem = path.stem
    if stem.startswith("yt_") and len(stem) > 3:
        return stem[3:]
    return stem


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


def load_ever_delivered() -> set[str]:
    """Permanent record — owner has already seen these Shorts in Telegram."""
    raw = _read_json(EVER_DELIVERED_PATH, {"ids": []})
    if not isinstance(raw, dict):
        return set()
    return {str(x) for x in raw.get("ids", [])}


def mark_ever_delivered(ids: list[str]) -> None:
    if not ids:
        return
    data = _read_json(EVER_DELIVERED_PATH, {"ids": []})
    if not isinstance(data, dict):
        data = {"ids": []}
    merged = load_ever_delivered() | {str(x) for x in ids if x}
    data["ids"] = sorted(merged)
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(EVER_DELIVERED_PATH, data)


def backfill_ever_delivered() -> int:
    """Merge sent_ids + labeled feedback into permanent delivery log."""
    before = len(load_ever_delivered())
    merged = load_ever_delivered()
    merged.update(load_feed_sent()["ids"])
    for row in load_labels().get("feedback", []):
        vid = str(row.get("video_id") or row.get("id") or "")
        if vid:
            merged.add(vid)
    payload = {
        "ids": sorted(merged),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_json(EVER_DELIVERED_PATH, payload)
    with _feed_sent_lock():
        sent = load_feed_sent()
        sent["ids"].update(merged)
        sent["file_ids"].update(merged)
        _write_feed_sent(sent)
    return len(merged) - before


def count_unlabeled_sent() -> dict[str, int]:
    labeled = labeled_ids()
    sent = load_feed_sent()
    missing = labeled_n = 0
    unlabeled = 0
    for vid in sent["ids"]:
        if vid in labeled:
            labeled_n += 1
            continue
        path = _expected_path(vid)
        if not path.exists() or path.stat().st_size < 10_000:
            missing += 1
            continue
        unlabeled += 1
    return {
        "sent_total": len(sent["ids"]),
        "unlabeled_on_disk": unlabeled,
        "labeled": labeled_n,
        "missing_file": missing,
    }


def recycle_unlabeled_sent(*, limit: int = 12) -> int:
    """Re-queue Shorts that were sent but never got 👍/👎 — disabled by default (causes repeats)."""
    if os.environ.get("MLBB_RECYCLE_SENT", "0") != "1":
        return 0
    labeled = labeled_ids()
    recycled: list[str] = []
    with _feed_sent_lock():
        sent = load_feed_sent()
        candidates = sorted(set(sent["ids"]) | set(sent.get("file_ids", set())))
        for vid in candidates:
            if vid in labeled:
                continue
            path = _expected_path(vid)
            if not path.exists() or path.stat().st_size < 10_000:
                continue
            recycled.append(vid)
            if len(recycled) >= limit:
                break
        if not recycled:
            diag = count_unlabeled_sent()
            print(
                f"recycle_none sent={diag['sent_total']} "
                f"unlabeled_on_disk={diag['unlabeled_on_disk']} "
                f"labeled={diag['labeled']} missing={diag['missing_file']}"
            )
            return 0
        for vid in recycled:
            sent["ids"].discard(vid)
            sent["file_ids"].discard(vid)
            sent["claimed"].pop(vid, None)
        _write_feed_sent(sent)
    for vid in recycled:
        path = _expected_path(vid)
        row = find_candidate(vid) or {}
        upsert_candidate(
            _backfill_short_metadata(
                {
                    **row,
                    "video_id": vid,
                    "id": vid,
                    "gameplay_pass": int(row.get("gameplay_pass") or 1),
                    "gameplay_score": float(row.get("gameplay_score") or 0.55),
                },
                path,
            )
        )
    return len(recycled)


def register_disk_short_candidate(vid: str, mp4: Path, row: dict | None = None) -> bool:
    """Put on-disk Short into queue only after correspondence + gameplay + owner score."""
    from gameplay_gate import is_mlbb_calibration_short
    from mlbb_correspondence import corresponds_to_mlbb_search, passes_owner_video_correspondence

    base = row or find_candidate(vid) or {}
    title = str(base.get("title", ""))
    query = str(base.get("search_query", ""))
    ok_corr, corr_reason = corresponds_to_mlbb_search(title=title, search_query=query)
    if not ok_corr:
        mark_feed_blocked(vid, reason=corr_reason, score=0.0)
        return False
    ok, gscore, greason = is_mlbb_calibration_short(mp4, description=title)
    if not ok:
        mark_feed_blocked(vid, reason=greason, score=gscore)
        return False
    owner_ok, owner_score = passes_owner_video_correspondence(mp4)
    if not owner_ok:
        oscore = float(owner_score) if owner_score == owner_score else 0.0
        mark_feed_blocked(vid, reason="low_owner_score", score=oscore)
        return False
    payload: dict = {
        **base,
        "video_id": vid,
        "id": vid,
        "title": base.get("title", vid),
        "url": base.get("url", f"https://www.youtube.com/shorts/{vid}"),
        "gameplay_pass": 1,
        "gameplay_score": round(float(gscore), 4),
        "gameplay_reason": greason,
    }
    if owner_score == owner_score:
        payload["owner_score"] = round(float(owner_score), 4)
        payload["owner_scored_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    upsert_candidate(_backfill_short_metadata(payload, mp4))
    return True


def refill_pending_emergency(*, limit: int = 15) -> int:
    """Register never-delivered disk Shorts only — never re-send already seen videos."""
    delivered = load_ever_delivered()
    added = 0
    if not SHORTS_ROOT.exists():
        return 0
    labeled = labeled_ids()
    for mp4 in sorted(SHORTS_ROOT.glob("yt_*.mp4")):
        if added >= limit:
            break
        if mp4.stat().st_size < 10_000:
            continue
        vid = id_from_path(mp4)
        if vid in delivered or vid in labeled:
            continue
        row = find_candidate(vid) or {}
        if register_disk_short_candidate(vid, mp4, row):
            added += 1
    if added:
        print(f"refill_new_on_disk={added}")
    return added


def mark_feed_sent(ids: list[str], *, paths: list[Path] | None = None) -> None:
    with _feed_sent_lock():
        sent = load_feed_sent()
        sent["ids"].update(str(x) for x in ids if x)
        for path in paths or []:
            if path.exists() or path.name.startswith("yt_"):
                sent["file_ids"].add(id_from_path(path))
                sent["ids"].add(id_from_path(path))
        for vid in ids:
            sent["claimed"].pop(str(vid), None)
        _write_feed_sent(sent)
    mark_ever_delivered([str(x) for x in ids if x])


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
    """Only one calibration feed process at a time."""
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


def claim_feed_candidates(rows: list[dict]) -> list[dict]:
    """Atomically reserve ids before Telegram send — prevents parallel duplicate feeds."""
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
    """Undo claim_feed_candidates for ids that were not actually delivered."""
    released = 0
    with _feed_sent_lock():
        sent = load_feed_sent()
        claimed_map: dict[str, str] = sent["claimed"]  # type: ignore[assignment]
        for raw in ids:
            vid = str(raw).strip()
            if vid.startswith("yt_"):
                vid = vid[3:]
            if not vid:
                continue
            for key in (vid, f"yt_{vid}"):
                if key in claimed_map:
                    claimed_map.pop(key, None)
                    released += 1
        if released:
            _write_feed_sent(sent)
    return released


def claimed_count() -> int:
    sent = load_feed_sent()
    claimed: dict[str, str] = sent.get("claimed", {})  # type: ignore[assignment]
    return len(claimed)


def last_feed_sent_age_sec() -> float:
    """Seconds since last successful feed delivery (survives worker restarts)."""
    raw = _read_json(FEED_SENT_PATH, {})
    if not isinstance(raw, dict):
        return 999999.0
    ts = str(raw.get("updated_at", "")).strip()
    if not ts:
        return 999999.0
    try:
        return max(0.0, time.time() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp())
    except ValueError:
        return 999999.0


def index_disk_avail() -> int:
    """Register on-disk Shorts not yet sent/labeled (same gates as ingest)."""
    sent = load_feed_sent()
    labels = labeled_ids()
    added = 0
    if not SHORTS_ROOT.exists():
        return 0
    for mp4 in SHORTS_ROOT.glob("yt_*.mp4"):
        if mp4.stat().st_size < 10_000:
            continue
        vid = id_from_path(mp4)
        if vid in sent["ids"] or vid in labels or vid in load_ever_delivered():
            continue
        row = find_candidate(vid) or {}
        if register_disk_short_candidate(vid, mp4, row):
            added += 1
    return added


def mark_feed_blocked(video_id: str, *, reason: str, score: float = 0.0) -> None:
    """Exclude repeat failures from pending queue after send-time gameplay gate."""
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


def release_stale_claims(*, max_age_sec: float = 2700) -> int:
    """Return reserved-but-undelivered ids to the pending queue (killed feed recovery)."""
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
            if vid in claimed_map:
                claimed_map.pop(vid, None)
                released += 1
        if released:
            _write_feed_sent(sent)
    return released


def release_all_claims() -> int:
    """Clear in-flight reservations (feed startup after crash)."""
    with _feed_sent_lock():
        sent = load_feed_sent()
        claimed_map: dict[str, str] = sent["claimed"]  # type: ignore[assignment]
        n = len(claimed_map)
        if n:
            sent["claimed"] = {}
            _write_feed_sent(sent)
        return n


def migrate_labels_from_paths() -> int:
    """Fix legacy rows where video_id != filename (duplicate-bug era)."""
    labels = load_labels()
    fixed = 0
    for section in ("good", "bad", "feedback"):
        for row in labels.get(section, []):
            path = Path(str(row.get("path", "")))
            if not path.name.startswith("yt_"):
                continue
            canon = id_from_path(path)
            if row.get("video_id") != canon or row.get("id") != canon:
                row["video_id"] = canon
                row["id"] = canon
                fixed += 1
    if fixed:
        save_labels(labels)
    return fixed


def labeled_ids() -> dict[str, str]:
    """video_id and file-canonical id -> good|bad"""
    migrate_labels_from_paths()
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
        label = row.get("owner_label")
        if label in ("yes", "good"):
            add_row(row, "good")
        elif label in ("no", "bad"):
            add_row(row, "bad")
    return out


def row_corresponds_to_mlbb(row: dict) -> str | None:
    """None if OK; else rejection reason (correspondence with MLBB search intent)."""
    from mlbb_correspondence import corresponds_to_mlbb_search

    title = str(row.get("title", ""))
    query = str(row.get("search_query", ""))
    ok, reason = corresponds_to_mlbb_search(title=title, search_query=query)
    return None if ok else reason


def purge_non_mlbb_candidates(*, limit: int | None = None) -> int:
    """Re-scan queue rows and mark non-MLBB / no-correspondence as gameplay_pass=0."""
    from gameplay_gate import is_mlbb_calibration_short

    cap = limit if limit is not None else int(os.environ.get("MLBB_PURGE_LIMIT", "200"))
    blocked = 0
    for row in load_index().get("candidates", []):
        if blocked >= cap:
            break
        if int(row.get("gameplay_pass") or 0) != 1:
            continue
        vid = str(row.get("video_id", "")).strip()
        if not vid:
            continue
        corr = row_corresponds_to_mlbb(row)
        if corr:
            mark_feed_blocked(vid, reason=corr, score=0.0)
            blocked += 1
            continue
        path = _expected_path(vid)
        if not path.exists():
            continue
        ok, score, reason = is_mlbb_calibration_short(path, description=str(row.get("title", "")))
        if ok:
            continue
        mark_feed_blocked(vid, reason=reason, score=score)
        blocked += 1
    return blocked


def ingest_sent_blocklist() -> set[str]:
    """IDs ingest should skip (already delivered to owner)."""
    return load_ever_delivered() | load_feed_sent()["ids"]


def _is_excluded(vid: str, path: Path, labeled: dict[str, str], sent: dict[str, set[str] | dict[str, str]]) -> bool:
    file_id = id_from_path(path)
    delivered = load_ever_delivered()
    claimed: dict[str, str] = sent.get("claimed", {})  # type: ignore[assignment]
    if vid in labeled or file_id in labeled:
        return True
    if vid in delivered or file_id in delivered:
        return True
    if vid in sent["ids"] or file_id in sent["ids"]:
        return True
    if file_id in sent["file_ids"]:
        return True
    if vid in claimed or file_id in claimed:
        return True
    return False


def find_candidate(video_id: str) -> dict | None:
    vid = video_id.strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    data = load_index()
    for row in data.get("candidates", []):
        row_vid = str(row.get("video_id", ""))
        path = Path(str(row.get("path", "")))
        if row_vid == vid or str(row.get("id", "")) == vid:
            return row
        if str(row_vid).startswith(vid):
            return row
        if path.name.startswith("yt_") and id_from_path(path) == vid:
            return row
    # Fallback: file on disk (owner pasted correct filename id)
    direct = SHORTS_ROOT / f"yt_{vid}.mp4"
    if direct.exists():
        return {
            "video_id": vid,
            "path": str(direct),
            "title": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "score": 0.0,
        }
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

    path = Path(row.get("path", ""))
    if not path.exists():
        path = SHORTS_ROOT / f"yt_{video_id.strip()}.mp4"
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
    def _same_file(row: dict) -> bool:
        p = Path(str(row.get("path", "")))
        return id_from_path(p) == vid if p.name.startswith("yt_") else row.get("video_id") == vid

    labels["feedback"] = [f for f in labels.get("feedback", []) if not _same_file(f)]
    labels["feedback"].append(feedback)

    if is_good:
        labels["good"] = [g for g in labels.get("good", []) if not _same_file(g)]
        labels["bad"] = [b for b in labels.get("bad", []) if not _same_file(b)]
        labels["good"].append(entry)
        exemplar = copy_exemplar(path, "good", vid)
        if exemplar:
            entry["exemplar"] = str(exemplar)
    else:
        labels["bad"] = [b for b in labels.get("bad", []) if not _same_file(b)]
        labels["good"] = [g for g in labels.get("good", []) if not _same_file(g)]
        labels["bad"].append(entry)
        exemplar = copy_exemplar(path, "bad", vid)
        if exemplar:
            entry["exemplar"] = str(exemplar)

    save_labels(labels)
    from mlbb_owner_learning import sync_shorts_label_to_owner_json

    sync_shorts_label_to_owner_json(vid, is_good=is_good, reason=reason)
    try:
        from mlbb_owner_feedback import record_owner_feedback

        record_owner_feedback(
            source="youtube_shorts",
            video_id=vid,
            time_sec=0.0,
            label="good" if is_good else "bad",
            reason=reason,
            item_id=vid,
        )
    except Exception:
        pass
    if not is_good:
        block_reason = reason or "owner_dislike"
        mark_feed_blocked(vid, reason=block_reason, score=0.0)
    return True, "good" if is_good else "bad"


def _expected_path(video_id: str) -> Path:
    return SHORTS_ROOT / f"yt_{video_id}.mp4"


def _backfill_short_metadata(row: dict, mp4: Path) -> dict:
    """Ensure queue freshness fields exist for disk/index rows."""
    out = {**row, "path": str(mp4)}
    if not _upload_date(out) and not str(out.get("ingested_at") or "").strip():
        try:
            out["ingested_at"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(mp4.stat().st_mtime)
            )
        except OSError:
            out["ingested_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return out


def backfill_gameplay_flags(*, limit: int = 50) -> int:
    """Tag candidates missing gameplay_pass so the pending queue can work."""
    from gameplay_gate import is_mlbb_calibration_short

    labeled = labeled_ids()
    sent = load_feed_sent()
    updated = 0
    for row in load_index().get("candidates", []):
        if updated >= limit:
            break
        if int(row.get("gameplay_pass") or 0) == 1:
            continue
        vid = str(row.get("video_id", "")).strip()
        if not vid or vid in labeled or vid in sent["ids"]:
            continue
        path = _expected_path(vid)
        if not path.exists() or path.stat().st_size < 10_000:
            continue
        ok, score, reason = is_mlbb_calibration_short(
            path, description=str(row.get("title", ""))
        )
        upsert_candidate(
            {
                **row,
                "video_id": vid,
                "id": vid,
                "path": str(path),
                "gameplay_pass": int(ok),
                "gameplay_score": round(float(score), 4),
                "gameplay_reason": reason,
            }
        )
        updated += 1
    return updated


def rebuild_index_from_disk(*, rescore: bool = False) -> int:
    """Re-register Shorts already on disk so owner can keep labeling after repair/prune."""
    added = 0
    if not SHORTS_ROOT.exists():
        return 0
    labeled = labeled_ids()
    for mp4 in sorted(SHORTS_ROOT.glob("yt_*.mp4")):
        if mp4.stat().st_size < 10_000:
            continue
        vid = id_from_path(mp4)
        if vid in labeled:
            continue
        row = find_candidate(vid) or {}
        if row.get("video_id") == vid and not rescore:
            upsert_candidate(
                _backfill_short_metadata(
                    {**row, "video_id": vid, "id": vid},
                    mp4,
                )
            )
            added += 1
            continue
        upsert_candidate(
            _backfill_short_metadata(
                {
                    "video_id": vid,
                    "id": vid,
                    "title": row.get("title", vid),
                    "url": row.get("url", f"https://www.youtube.com/shorts/{vid}"),
                    "score": float(row.get("score") or 0.12),
                    "source": "disk_rebuild",
                    "gameplay_pass": int(row.get("gameplay_pass") or 1),
                    "gameplay_score": float(row.get("gameplay_score") or 0.55),
                    "gameplay_reason": str(row.get("gameplay_reason") or "disk_rebuild"),
                },
                mp4,
            )
        )
        added += 1
    return added


def repair_index() -> int:
    """Drop corrupt rows (path/video_id mismatch) and duplicate video_ids."""
    data = load_index()
    rows = data.get("candidates", [])
    best: dict[str, dict] = {}
    removed = 0
    for row in rows:
        vid = str(row.get("video_id", "")).strip()
        if not vid or len(vid) != 11:
            removed += 1
            continue
        path = Path(row.get("path", ""))
        expected = _expected_path(vid)
        if path.name != expected.name or not expected.exists():
            removed += 1
            continue
        row = {**row, "path": str(expected)}
        prev = best.get(vid)
        if prev is None or float(row.get("score") or 0) >= float(prev.get("score") or 0):
            best[vid] = row
        else:
            removed += 1
    old_n = len(rows)
    data["candidates"] = list(best.values())
    save_index(data)
    return old_n - len(data["candidates"])


def _upload_date(row: dict) -> str:
    ud = str(row.get("upload_date") or "").strip()
    if ud.isdigit() and len(ud) >= 8:
        return ud
    return ""


def is_fresh_short(row: dict) -> bool:
    """Reject legacy low-quality Shorts (e.g. 2020) from the calibration queue."""
    min_year = int(os.environ.get("MLBB_SHORTS_MIN_YEAR", "2024"))
    max_days = int(os.environ.get("MLBB_SHORTS_DAYS", "60"))
    ud = _upload_date(row)
    if not ud:
        ingested_at = str(row.get("ingested_at") or "").strip()
        if ingested_at:
            try:
                ts = time.strptime(ingested_at, "%Y-%m-%d %H:%M:%S")
                age_days = (time.time() - time.mktime(ts)) / 86400.0
                if age_days <= max_days:
                    return True
            except ValueError:
                pass
        return os.environ.get("MLBB_SHORTS_REQUIRE_DATE", "1") != "1"
    if int(ud[:4]) < min_year:
        return False
    if os.environ.get("MLBB_SHORTS_YEAR_ONLY", "1") == "1":
        return True
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).strftime("%Y%m%d")
    return ud >= cutoff


def _freshness_sort_key(row: dict) -> tuple[str, float]:
    ud = _upload_date(row) or "00000000"
    return (ud, float(row.get("score") or 0))


def _exemplar_counts() -> tuple[int, int]:
    root = EXEMPLAR_ROOT / "mobile_legends"
    good = len(list((root / "good").glob("*.mp4"))) if (root / "good").exists() else 0
    bad = len(list((root / "bad").glob("*.mp4"))) if (root / "bad").exists() else 0
    return good, bad


def owner_rank_enabled() -> bool:
    if os.environ.get("MLBB_OWNER_RANK", "1") != "1":
        return False
    good, bad = _exemplar_counts()
    min_total = int(os.environ.get("MLBB_OWNER_MIN_EXEMPLARS", "50"))
    return (good + bad) >= min_total


def compute_owner_score(path: Path) -> float:
    """CLIP similarity to owner 👍/👎 exemplars — drives pending queue ranking."""
    from mlbb_telegram_video import probe_duration

    try:
        from highlight_scorer import score_clip_exemplar
    except ImportError:
        return float("nan")
    dur = max(3.0, min(12.0, probe_duration(path) or 12.0))
    try:
        score, _frames = score_clip_exemplar(path, 0.15, dur, "mobile_legends")
    except Exception:
        return float("nan")
    return round(float(score), 4)


def rescore_pending_candidates(*, limit: int | None = None) -> int:
    """Re-score unevaluated Shorts against owner exemplars after 👍/👎 votes."""
    if not owner_rank_enabled():
        return 0
    cap = limit if limit is not None else int(os.environ.get("MLBB_RESCORE_LIMIT", "30"))
    labeled = labeled_ids()
    sent = load_feed_sent()
    updated = 0
    for row in load_index().get("candidates", []):
        if updated >= cap:
            break
        vid = str(row.get("video_id", "")).strip()
        if not vid or vid in labeled or vid in sent["ids"]:
            continue
        if int(row.get("gameplay_pass") or 0) != 1:
            continue
        path = _expected_path(vid)
        if not path.exists():
            continue
        owner_score = compute_owner_score(path)
        if owner_score != owner_score:  # NaN
            continue
        upsert_candidate(
            {
                **row,
                "video_id": vid,
                "owner_score": owner_score,
                "owner_scored_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        updated += 1
    return updated


def _pending_sort_key(row: dict) -> tuple[float, str, float]:
    ud = _upload_date(row) or "00000000"
    if owner_rank_enabled():
        raw = row.get("owner_score")
        owner = float(raw) if raw is not None else -999.0
        return (owner, ud, float(row.get("gameplay_score") or 0))
    ud_key, score = _freshness_sort_key(row)
    return (float(score), ud_key, float(row.get("gameplay_score") or 0))


def sync_owner_learning(*, rescore_limit: int | None = None) -> dict:
    """After 👍/👎 or train: bust CLIP cache and refresh pending owner_score."""
    try:
        from highlight_scorer import clear_exemplar_cache
    except ImportError:
        clear_exemplar_cache = None  # type: ignore[assignment,misc]
    if clear_exemplar_cache:
        clear_exemplar_cache()
    n = rescore_pending_candidates(limit=rescore_limit)
    return {"rescored": n, "owner_rank": owner_rank_enabled()}


def _row_passes_owner_gate(row: dict) -> bool:
    if not owner_rank_enabled():
        return True
    if os.environ.get("MLBB_OWNER_EMERGENCY", "0") == "1":
        return True
    raw = row.get("owner_score")
    if raw is None:
        return os.environ.get("MLBB_OWNER_REQUIRE_SCORE", "1") != "1"
    floor = float(os.environ.get("MLBB_OWNER_SCORE_MIN", "-0.08"))
    return float(raw) >= floor


def _ensure_owner_scores(rows: list[dict], *, limit: int) -> None:
    if not owner_rank_enabled():
        return
    need = [r for r in rows if r.get("owner_score") is None][:limit]
    for row in need:
        path = Path(row.get("path", ""))
        if not path.exists():
            continue
        score = compute_owner_score(path)
        if score != score:
            continue
        row["owner_score"] = score
        upsert_candidate(
            {
                **(find_candidate(str(row.get("video_id", ""))) or row),
                "video_id": row.get("video_id"),
                "owner_score": score,
                "owner_scored_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )


def _row_passes_pending_gate(row: dict, path: Path) -> bool:
    """Fast queue gate — MLBB correspondence + gameplay metadata."""
    if int(row.get("gameplay_pass") or 0) != 1:
        return False
    if row_corresponds_to_mlbb(row):
        return False
    min_views = int(os.environ.get("MLBB_SHORTS_MIN_VIEWS", "50"))
    if int(row.get("view_count") or 0) < min_views:
        return False
    gscore = float(row.get("gameplay_score") or 0)
    if gscore < float(os.environ.get("MLBB_CALIBRATION_MIN_HEURISTIC", "0.52")):
        return False
    return True


def pending_candidates(*, limit: int = 50, repair: bool = True) -> list[dict]:
    if repair and os.environ.get("MLBB_PENDING_SKIP_REPAIR", "0") != "1":
        repair_index()
    migrate_labels_from_paths()
    labeled = labeled_ids()
    sent = load_feed_sent()
    rows = load_index().get("candidates", [])
    out: list[dict] = []
    seen_vids: set[str] = set()
    seen_paths: set[str] = set()
    for row in rows:
        path = _expected_path(str(row.get("video_id", "")))
        if not path.exists():
            continue
        vid = id_from_path(path)
        if _is_excluded(vid, path, labeled, sent) or vid in seen_vids:
            continue
        if not is_fresh_short(row):
            continue
        if not _row_passes_pending_gate(row, path):
            continue
        path_key = str(path.resolve())
        if path_key in seen_paths:
            continue
        seen_vids.add(vid)
        seen_paths.add(path_key)
        out.append({**row, "video_id": vid, "id": vid, "path": str(path)})
    score_limit = int(os.environ.get("MLBB_OWNER_SCORE_ON_DEMAND", "0"))
    if score_limit > 0 and os.getloadavg()[0] < float(os.environ.get("MLBB_OWNER_SCORE_LOAD_MAX", "12")):
        _ensure_owner_scores(out, limit=score_limit)
    out = [r for r in out if _row_passes_owner_gate(r)]
    out.sort(key=_pending_sort_key, reverse=True)
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
    good_ex, bad_ex = _exemplar_counts()
    return {
        "feedback_yes": yes,
        "feedback_no": no,
        "accuracy": round(accuracy, 4),
        "comparable": comparable,
        "good_labels": len(labels.get("good", [])),
        "bad_labels": len(labels.get("bad", [])),
        "good_exemplars": good_ex,
        "bad_exemplars": bad_ex,
        "owner_rank": owner_rank_enabled(),
        "index_total": len(load_index().get("candidates", [])),
        "pending": len(pending_candidates(limit=9999, repair=False)),
    }


def ready_for_eval(*, min_yes: int = 30, min_no: int = 20) -> bool:
    s = stats()
    return s["feedback_yes"] >= min_yes and s["feedback_no"] >= min_no


# Owner 👎 reason codes (stored in calibration_labels.json → highlight_train).
DISLIKE_REASONS: tuple[tuple[str, str], ...] = (
    ("promo", "📢 Реклама"),
    ("not_gameplay", "🎬 Не геймплей"),
    ("boring", "😴 Скучно"),
    ("wrong_hero", "🦸 Не тот герой"),
    ("no_kill", "💀 Нет килла / баннера"),
    ("single_only", "1️⃣ Только 1 килл"),
    ("music", "🎵 Музыка"),
    ("blurry", "🌫 Мыльное"),
    ("other", "🗑 Другое"),
)

DISLIKE_REASON_CODES = {code for code, _ in DISLIKE_REASONS}


def dislike_reason_label(reason: str) -> str:
    for code, label in DISLIKE_REASONS:
        if code == reason:
            return label
    return reason.strip() or "Плохо"


def dislike_reason_keyboard_markup(item_id: str, *, callback_prefix: str = "mlbb_bad") -> dict:
    """Eight reason buttons shown after 👎 (second step)."""
    vid = str(item_id).strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for code, label in DISLIKE_REASONS:
        row.append({"text": label, "callback_data": f"{callback_prefix}:{vid}:{code}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


def inline_keyboard_markup(video_id: str) -> dict:
    """Telegram inline keyboard: 👍 / 👎 under calibration Shorts."""
    vid = str(video_id).strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    return {
        "inline_keyboard": [
            [
                {"text": "👍", "callback_data": f"mlbb_yes:{vid}"},
                {"text": "👎", "callback_data": f"mlbb_no:{vid}"},
            ],
        ]
    }


def labeled_keyboard_markup(label: str, *, reason: str = "", video_id: str = "") -> dict:
    if label == "good":
        vid = str(video_id).strip()
        if vid.startswith("yt_"):
            vid = vid[3:]
        rows: list[list[dict]] = [[{"text": "✅ Хорошо", "callback_data": "mlbb_noop"}]]
        if vid:
            rows.append([{"text": "📁 HQ файл", "callback_data": f"mlbb_hq:{vid}"}])
        return {"inline_keyboard": rows}
    mark = f"❌ {dislike_reason_label(reason)}"
    return {"inline_keyboard": [[{"text": mark, "callback_data": "mlbb_noop"}]]}
