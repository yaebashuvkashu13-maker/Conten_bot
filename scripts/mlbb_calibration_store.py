#!/usr/bin/env python3
"""Shared store for MLBB YouTube Shorts calibration (index, labels, exemplars)."""

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
SHORTS_ROOT = Path(os.environ.get("MLBB_SHORTS_ROOT", "/root/datasets/mlbb/youtube_shorts"))
YT_SHORT_FILE_RE = re.compile(r"^yt_[\w-]{11}\.mp4$")
EXEMPLAR_ROOT = Path(
    os.environ.get(
        "HIGHLIGHT_EXEMPLAR_ROOT",
        str(REPO / "data" / "highlight_exemplars"),
    )
)

INDEX_PATH = Path(os.environ.get("MLBB_SHORTS_INDEX", str(DATA_MLBB / "youtube_shorts_index.json")))
INGEST_SKIP_PATH = Path(os.environ.get("MLBB_INGEST_SKIP_PATH", str(DATA_MLBB / "ingest_skip_ids.json")))
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
    if is_stub_candidate(row) and not row.get("ingest_verified"):
        return
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


def _normalize_vid(video_id: str) -> str:
    vid = video_id.strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    return vid


def load_ingest_skip() -> dict[str, str]:
    raw = _read_json(INGEST_SKIP_PATH, {"ids": {}, "updated_at": ""})
    ids = raw.get("ids", {})
    if not isinstance(ids, dict):
        return {}
    return {str(k): str(v) for k, v in ids.items()}


def mark_ingest_skip(video_id: str, reason: str = "") -> None:
    """Remember ids that failed download/gates so ingest stops retrying them."""
    vid = _normalize_vid(video_id)
    if not vid or len(vid) != 11:
        return
    data = _read_json(INGEST_SKIP_PATH, {"ids": {}, "updated_at": ""})
    ids = data.setdefault("ids", {})
    if not isinstance(ids, dict):
        ids = {}
        data["ids"] = ids
    ids[vid] = reason or ids.get(vid) or "skip"
    if len(ids) > 5000:
        for key in list(ids.keys())[:2500]:
            ids.pop(key, None)
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(INGEST_SKIP_PATH, data)


def ingest_skip_ids() -> set[str]:
    """All video ids ingest should not retry (labeled, sent, rejected, unavailable)."""
    skip = ingest_pool_skip_ids()
    ds_path = Path(os.environ.get("MLBB_DOWNLOAD_STATE", str(DATA_MLBB / "download_state.json")))
    if ds_path.exists():
        try:
            ds = json.loads(ds_path.read_text(encoding="utf-8"))
            for key in ("rejected_ids", "downloaded_ids"):
                for item in ds.get(key, []):
                    if isinstance(item, str) and len(item) == 11:
                        skip.add(item)
        except (json.JSONDecodeError, OSError):
            pass
    return skip


def ingest_pool_skip_ids() -> set[str]:
    """Ids to exclude from YouTube search pool — not every historical download."""
    skip = set(load_ingest_skip().keys())
    skip.update(labeled_ids().keys())
    skip.update(load_feed_sent()["ids"])
    return skip


def load_feed_sent() -> dict[str, set[str]]:
    raw = _read_json(FEED_SENT_PATH, {"sent_ids": [], "sent_file_ids": []})
    if not isinstance(raw, dict):
        return {"ids": set(), "file_ids": set()}
    return {
        "ids": set(str(x) for x in raw.get("sent_ids", [])),
        "file_ids": set(str(x) for x in raw.get("sent_file_ids", [])),
    }


def mark_feed_sent(ids: list[str], *, paths: list[Path] | None = None) -> None:
    sent = load_feed_sent()
    sent["ids"].update(str(x) for x in ids if x)
    for path in paths or []:
        if path.exists() or path.name.startswith("yt_"):
            sent["file_ids"].add(id_from_path(path))
            sent["ids"].add(id_from_path(path))
    _write_json(
        FEED_SENT_PATH,
        {
            "sent_ids": sorted(sent["ids"]),
            "sent_file_ids": sorted(sent["file_ids"]),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


def reject_candidate(video_id: str, *, reason: str = "", path: Path | None = None) -> None:
    """Drop non-MLBB / bad queue rows so they are not sent again."""
    vid = str(video_id).strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    data = load_index()
    data["candidates"] = [
        row for row in data.get("candidates", []) if str(row.get("video_id", "")) != vid
    ]
    save_index(data)
    mark_feed_sent([vid], paths=[path] if path else None)

    labels = load_labels()
    file_path = path or _expected_path(vid)
    entry = {
        "video_id": vid,
        "id": vid,
        "path": str(file_path),
        "title": vid,
        "reason": reason or "auto_reject",
        "source": "auto_reject",
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    def _same_file(row: dict) -> bool:
        p = Path(str(row.get("path", "")))
        return id_from_path(p) == vid if p.name.startswith("yt_") else row.get("video_id") == vid

    labels["bad"] = [b for b in labels.get("bad", []) if not _same_file(b)]
    labels["bad"].append(entry)
    labels["feedback"] = [f for f in labels.get("feedback", []) if not _same_file(f)]
    labels["feedback"].append({**entry, "owner_label": "no", "model_score": 0})
    save_labels(labels)
    if file_path.exists():
        copy_exemplar(file_path, "bad", vid)


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


def _is_excluded(vid: str, path: Path, labeled: dict[str, str], sent: dict[str, set[str]]) -> bool:
    file_id = id_from_path(path)
    if vid in labeled or file_id in labeled:
        return True
    if vid in sent["ids"] or file_id in sent["ids"]:
        return True
    if file_id in sent["file_ids"]:
        return True
    return False


def find_labeled_row(video_id: str) -> dict | None:
    """Row from owner labels/history — for 👍/👎 on clips no longer in index."""
    vid = _normalize_vid(video_id)
    labels = load_labels()
    for section in ("feedback", "good", "bad"):
        for row in labels.get(section, []):
            row_vid = str(row.get("video_id") or row.get("id") or "").strip()
            path = Path(str(row.get("path", "")))
            file_vid = id_from_path(path) if path.name.startswith("yt_") else row_vid
            if row_vid == vid or file_vid == vid:
                return row
    return None


def find_candidate_or_labeled(video_id: str) -> dict | None:
    row = find_candidate(video_id)
    if row and not is_stub_candidate(row):
        return row
    row = find_labeled_row(video_id)
    if row:
        return row
    vid = _normalize_vid(video_id)
    direct = SHORTS_ROOT / f"yt_{vid}.mp4"
    if direct.exists() and direct.stat().st_size > 10_000:
        prev = find_labeled_row(vid) or {}
        return {
            "video_id": vid,
            "id": vid,
            "path": str(direct),
            "title": prev.get("title", ""),
            "url": prev.get("url", f"https://www.youtube.com/watch?v={vid}"),
            "score": prev.get("score", 0),
        }
    return None


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
        if path.name.startswith("yt_") and id_from_path(path) == vid:
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
    row = find_candidate_or_labeled(video_id)
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
        try:
            from mlbb_training_archive import archive_short

            archived = archive_short(
                path,
                vid,
                upload_date=str(row.get("upload_date", "")),
                title=str(row.get("title", "")),
            )
            if archived:
                entry["training_archive"] = str(archived)
        except ImportError:
            pass
    else:
        labels["bad"] = [b for b in labels.get("bad", []) if not _same_file(b)]
        labels["good"] = [g for g in labels.get("good", []) if not _same_file(g)]
        labels["bad"].append(entry)
        exemplar = copy_exemplar(path, "bad", vid)
        if exemplar:
            entry["exemplar"] = str(exemplar)

    try:
        from mlbb_scene_library import register_shorts_label

        register_shorts_label(
            path=path,
            video_id=vid,
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
    return True, "good" if is_good else "bad"


def _expected_path(video_id: str) -> Path:
    return SHORTS_ROOT / f"yt_{video_id}.mp4"


def _is_merged_short(path: Path) -> bool:
    """Skip yt-dlp partials like yt_{id}.f299.mp4 (race during merge)."""
    stem = path.stem
    if not stem.startswith("yt_"):
        return False
    return ".f" in stem[3:]


def rebuild_index_from_disk(*, rescore: bool = False) -> int:
    """Re-register Shorts already on disk so owner can keep labeling after repair/prune."""
    added = 0
    if not SHORTS_ROOT.exists():
        return 0
    labeled = labeled_ids()
    for mp4 in sorted(SHORTS_ROOT.glob("yt_*.mp4")):
        if not YT_SHORT_FILE_RE.match(mp4.name):
            continue
        if _is_merged_short(mp4):
            continue
        try:
            size = mp4.stat().st_size
        except OSError:
            continue
        if size < 10_000:
            continue
        vid = id_from_path(mp4)
        if vid in labeled:
            continue
        row = find_candidate(vid) or {}
        if row.get("video_id") == vid and not rescore:
            if is_stub_candidate(row) or not row.get("ingest_verified"):
                continue
            upsert_candidate({**row, "path": str(mp4), "video_id": vid, "id": vid})
            added += 1
            continue
        # Unknown disk files must go through ingest gates — never fake score=0.12 into queue.
        continue
    return added


def rescue_unindexed_shorts(*, limit: int = 6) -> int:
    """Register downloaded mp4 stuck on disk (ingest hung before upsert)."""
    return index_unlabeled_disk_shorts(limit=limit)


def index_unlabeled_disk_shorts(*, limit: int = 24, max_sec: float | None = None) -> int:
    """Index unlabeled Shorts already on disk so feed can send without re-download."""
    if not SHORTS_ROOT.exists():
        return 0
    import subprocess

    from mlbb_youtube_shorts_ingest import passes_mlbb_shorts_activity_gate, shorts_short_max_sec

    max_dur = float(max_sec if max_sec is not None else os.environ.get("MLBB_SHORTS_MAX_DURATION_SEC", "60"))
    if os.environ.get("MLBB_SHORTS_ONLY", "0") == "1":
        max_dur = min(max_dur, shorts_short_max_sec())
    labeled = labeled_ids()
    sent = load_feed_sent()
    added = 0
    for mp4 in sorted(SHORTS_ROOT.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        if added >= limit:
            break
        if not YT_SHORT_FILE_RE.match(mp4.name) or _is_merged_short(mp4):
            continue
        try:
            size = mp4.stat().st_size
        except OSError:
            continue
        if size < 10_000 or size > int(os.environ.get("MLBB_RESCUE_MAX_BYTES", str(80 * 1024 * 1024))):
            continue
        vid = id_from_path(mp4)
        if vid in labeled or vid in sent["ids"]:
            continue
        existing = find_candidate(vid) or {}
        if existing.get("ingest_verified") and not is_stub_candidate(existing):
            if vid not in labeled and vid not in sent["ids"]:
                upsert_candidate({**existing, "path": str(mp4), "video_id": vid, "id": vid})
                added += 1
            continue
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(mp4),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        try:
            dur = float((proc.stdout or "0").strip() or 0)
        except ValueError:
            dur = 0.0
        if dur > 0 and dur > max_dur:
            continue
        act_ok, act_reason = passes_mlbb_shorts_activity_gate(mp4)
        if not act_ok:
            continue
        score = float(existing.get("score") or 0.35)
        title = str(existing.get("title") or vid)
        url = existing.get("url") or f"https://www.youtube.com/watch?v={vid}"
        if title == vid or len(title) <= 12:
            try:
                import json as _json
                import urllib.request as _urllib

                oembed_url = (
                    f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json"
                )
                with _urllib.urlopen(oembed_url, timeout=10) as resp:
                    meta = _json.loads(resp.read().decode())
                if meta.get("title"):
                    title = str(meta["title"])
            except Exception:
                pass
        upsert_candidate(
            {
                **existing,
                "video_id": vid,
                "path": str(mp4),
                "title": title,
                "url": url,
                "score": score,
                "clip_start_sec": float(existing.get("clip_start_sec") or 0.15),
                "gameplay_pass": 1,
                "identity_pass": 1,
                "ingest_verified": 1,
                "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        added += 1
        print(f"indexed_disk {vid} dur={dur:.0f}s score={score:.3f}", flush=True)
    return added


def repair_index() -> int:
    """Drop corrupt rows, duplicates, and legacy stub entries."""
    data = load_index()
    rows = data.get("candidates", [])
    best: dict[str, dict] = {}
    removed = 0
    for row in rows:
        vid = str(row.get("video_id", "")).strip()
        if not vid or len(vid) != 11:
            removed += 1
            continue
        if is_stub_candidate(row):
            removed += 1
            try:
                _expected_path(vid).unlink(missing_ok=True)
            except OSError:
                pass
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


_LAST_INDEX_REPAIR = 0.0


def is_stub_candidate(row: dict) -> bool:
    """Legacy disk file without YouTube ingest metadata (title=id, score=0)."""
    vid = str(row.get("video_id") or row.get("id") or "").strip()
    title = str(row.get("title") or "").strip()
    if not vid or len(vid) != 11:
        return True
    if row.get("ingest_verified"):
        return False
    if title == vid or not title:
        return True
    if float(row.get("score") or 0) <= 0 and not row.get("ingested_at"):
        return True
    return False


def pending_candidates(*, limit: int = 50, repair: bool = True) -> list[dict]:
    global _LAST_INDEX_REPAIR
    if repair:
        interval = float(os.environ.get("MLBB_INDEX_REPAIR_SEC", "90"))
        now = time.time()
        if now - _LAST_INDEX_REPAIR >= interval:
            repair_index()
            _LAST_INDEX_REPAIR = now
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
        path_key = str(path.resolve())
        if path_key in seen_paths:
            continue
        if is_stub_candidate(row):
            continue
        seen_vids.add(vid)
        seen_paths.add(path_key)
        out.append({**row, "video_id": vid, "id": vid, "path": str(path)})
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
            ]
        ]
    }


def good_download_keyboard_markup(video_id: str) -> dict:
    """After 👍 — offer HQ download."""
    vid = str(video_id).strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    return {
        "inline_keyboard": [
            [{"text": "📥 Скачать оригинал", "callback_data": f"mlbb_hq_shorts:{vid}"}]
        ]
    }


def labeled_keyboard_markup(label: str, *, video_id: str = "", segment_id: str = "") -> dict:
    if label == "good":
        mark = "✅ Отправлено"
    else:
        mark = "❌ Плохо"
    return {"inline_keyboard": [[{"text": mark, "callback_data": "mlbb_noop"}]]}
