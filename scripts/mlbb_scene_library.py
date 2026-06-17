#!/usr/bin/env python3
"""Scene library — index every owner 👍/👎 for future montage / micro-clip synthesis."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def library_enabled() -> bool:
    return os.environ.get("MLBB_SCENE_LIBRARY", "1") == "1"


def index_path() -> Path:
    data = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    return Path(os.environ.get("MLBB_SCENE_LIBRARY_INDEX", str(data / "scene_library_index.jsonl")))


def stats_path() -> Path:
    data = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    return Path(os.environ.get("MLBB_SCENE_LIBRARY_STATS", str(data / "scene_library_stats.json")))


def make_scene_id(*, source: str, video_id: str = "", segment_id: str = "", start_sec: float = 0.0) -> str:
    if source == "vod_segment" and segment_id:
        return f"vod:{segment_id.strip()}"
    vid = video_id.strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    start = max(0.0, float(start_sec or 0.0))
    return f"shorts:{vid}:{start:.2f}"


def classify_scene_type(*, source: str, row: dict | None = None, reason: str = "") -> str:
    row = row or {}
    blob = " ".join(
        str(row.get(k, ""))
        for k in (
            "pass_reason",
            "gate_reason",
            "reason",
            "title",
        )
    ).lower()
    blob = f"{blob} {reason}".lower()
    if "kill" in blob or "savage" in blob or "maniac" in blob:
        return "kill"
    if "teamfight" in blob or "fight" in blob:
        return "teamfight"
    if "chase" in blob or "gank" in blob:
        return "chase"
    if source == "vod_segment":
        return "fight"
    return "gameplay"


def _probe_duration(path: Path) -> float:
    if not path.exists():
        return 0.0
    try:
        import subprocess

        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        return max(0.0, float((proc.stdout or "0").strip() or 0))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def register_scene(
    *,
    source: str,
    path: Path,
    owner_label: str,
    video_id: str = "",
    segment_id: str = "",
    start_sec: float = 0.0,
    end_sec: float = 0.0,
    peak_sec: float = 0.0,
    duration_sec: float = 0.0,
    score: float = 0.0,
    hook_score: float = 0.0,
    scene_type: str = "",
    title: str = "",
    url: str = "",
    upload_date: str = "",
    archive_path: str = "",
    exemplar_path: str = "",
    reason: str = "",
    by_chat: str = "",
    extra: dict | None = None,
) -> dict | None:
    """Append one labeled scene row to the library index."""
    if not library_enabled() or not path.exists():
        return None

    dur = duration_sec or _probe_duration(path)
    if end_sec <= start_sec and dur > 0:
        end_sec = start_sec + dur

    sid = make_scene_id(
        source=source,
        video_id=video_id,
        segment_id=segment_id,
        start_sec=start_sec,
    )
    row = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scene_id": sid,
        "source": source,
        "video_id": video_id,
        "segment_id": segment_id,
        "owner_label": owner_label,
        "scene_type": scene_type
        or classify_scene_type(source=source, row={"title": title, **(extra or {})}, reason=reason),
        "start_sec": round(float(start_sec or 0.0), 3),
        "end_sec": round(float(end_sec or 0.0), 3),
        "peak_sec": round(float(peak_sec or start_sec or 0.0), 3),
        "duration_sec": round(float(dur or 0.0), 3),
        "score": float(score or 0.0),
        "hook_score": float(hook_score or 0.0),
        "title": (title or "")[:240],
        "url": url or "",
        "upload_date": upload_date or "",
        "path": str(path),
        "archive_path": archive_path or "",
        "exemplar_path": exemplar_path or "",
        "reason": reason or "",
        "by_chat": by_chat or "",
        **(extra or {}),
    }

    idx = index_path()
    idx.parent.mkdir(parents=True, exist_ok=True)
    with idx.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    _bump_stats(row)
    return row


def register_shorts_label(
    *,
    path: Path,
    video_id: str,
    is_good: bool,
    row: dict,
    reason: str = "",
    by_chat: str = "",
    archive_path: str = "",
    exemplar_path: str = "",
) -> dict | None:
    start = float(row.get("clip_start_sec") or row.get("trim_start_sec") or 0.15)
    return register_scene(
        source="youtube_shorts",
        path=path,
        owner_label="yes" if is_good else "no",
        video_id=video_id,
        start_sec=start,
        score=float(row.get("score") or 0.0),
        hook_score=float(row.get("hook_score") or 0.0),
        title=str(row.get("title") or ""),
        url=str(row.get("url") or ""),
        upload_date=str(row.get("upload_date") or ""),
        archive_path=archive_path,
        exemplar_path=exemplar_path,
        reason=reason,
        by_chat=by_chat,
        extra={
            "ingest_verified": bool(row.get("ingest_verified")),
            "gameplay_pass": row.get("gameplay_pass"),
        },
    )


def register_vod_label(
    *,
    path: Path,
    segment_id: str,
    is_good: bool,
    row: dict,
    reason: str = "",
    by_chat: str = "",
    archive_path: str = "",
    exemplar_path: str = "",
) -> dict | None:
    start = float(row.get("start") or 0.0)
    peak = float(row.get("peak_start") or start)
    fight_end = float(row.get("fight_end") or row.get("fight_dur") or 0.0)
    end = fight_end if fight_end > start else start + float(row.get("output_duration") or 15.0)
    return register_scene(
        source="vod_segment",
        path=path,
        owner_label="yes" if is_good else "no",
        video_id=str(row.get("vod_id") or row.get("vod") or segment_id.rsplit("_", 1)[0]),
        segment_id=segment_id,
        start_sec=start,
        end_sec=end,
        peak_sec=peak,
        score=float(row.get("score") or 0.0),
        hook_score=float(row.get("hook_score") or 0.0),
        reason=reason,
        by_chat=by_chat,
        archive_path=archive_path,
        exemplar_path=exemplar_path,
        extra={
            "gate_reason": row.get("gate_reason") or row.get("pass_reason") or "",
            "vod_id": row.get("vod_id") or row.get("vod") or "",
        },
    )


def _bump_stats(row: dict) -> None:
    sp = stats_path()
    data: dict = {}
    if sp.exists():
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    data.setdefault("total", 0)
    data.setdefault("good", 0)
    data.setdefault("bad", 0)
    data.setdefault("by_source", {})
    data.setdefault("by_scene_type", {})
    data["total"] = int(data.get("total", 0)) + 1
    label = row.get("owner_label", "")
    if label in ("yes", "good"):
        data["good"] = int(data.get("good", 0)) + 1
    elif label in ("no", "bad"):
        data["bad"] = int(data.get("bad", 0)) + 1
    src = str(row.get("source", "unknown"))
    data["by_source"][src] = int(data["by_source"].get(src, 0)) + 1
    st = str(row.get("scene_type", "unknown"))
    data["by_scene_type"][st] = int(data["by_scene_type"].get(st, 0)) + 1
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_stats() -> dict:
    sp = stats_path()
    if not sp.exists():
        return {"total": 0, "good": 0, "bad": 0, "by_source": {}, "by_scene_type": {}}
    try:
        return json.loads(sp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"total": 0, "good": 0, "bad": 0, "by_source": {}, "by_scene_type": {}}


def list_good_scenes(*, limit: int = 100, source: str = "") -> list[dict]:
    idx = index_path()
    if not idx.exists():
        return []
    out: list[dict] = []
    for line in idx.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("owner_label") not in ("yes", "good"):
            continue
        if source and row.get("source") != source:
            continue
        if not Path(str(row.get("path", ""))).exists():
            continue
        out.append(row)
    return out[-limit:]


def backfill_from_labels(*, skip_existing: bool = True) -> int:
    """Import historical labels into scene_library_index (one-time / repair)."""
    if not library_enabled():
        return 0
    existing: set[str] = set()
    if skip_existing and index_path().exists():
        for line in index_path().read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                existing.add(str(row.get("scene_id", "")))
            except json.JSONDecodeError:
                continue

    added = 0
    try:
        from mlbb_calibration_store import SHORTS_ROOT, load_labels as load_short_labels
    except ImportError:
        load_short_labels = None  # type: ignore
        SHORTS_ROOT = Path("/root/datasets/mlbb/youtube_shorts")

    if load_short_labels:
        labels = load_short_labels()
        for section, is_good in (("good", True), ("bad", False)):
            for row in labels.get(section, []):
                path = Path(str(row.get("path", "")))
                if not path.exists() and str(row.get("video_id", "")):
                    vid = str(row.get("video_id", ""))
                    path = SHORTS_ROOT / f"yt_{vid}.mp4"
                if not path.exists():
                    continue
                vid = str(row.get("video_id") or row.get("id") or "")
                sid = make_scene_id(source="youtube_shorts", video_id=vid, start_sec=0.15)
                if sid in existing:
                    continue
                register_shorts_label(
                    path=path,
                    video_id=vid,
                    is_good=is_good,
                    row=row,
                    reason=str(row.get("reason") or ""),
                    archive_path=str(row.get("training_archive") or ""),
                    exemplar_path=str(row.get("exemplar") or ""),
                )
                existing.add(sid)
                added += 1

    try:
        from mlbb_vod_segment_store import load_labels as load_vod_labels, _segments_root
    except ImportError:
        load_vod_labels = None  # type: ignore
        _segments_root = lambda: Path("/root/datasets/mlbb/vod_segments")  # type: ignore

    if load_vod_labels:
        labels = load_vod_labels()
        for section, is_good in (("good", True), ("bad", False)):
            for row in labels.get(section, []):
                seg = str(row.get("segment_id") or "")
                if not seg:
                    continue
                path = Path(str(row.get("path", "")))
                if not path.exists():
                    path = _segments_root() / f"seg_{seg}.mp4"
                if not path.exists():
                    continue
                sid = make_scene_id(source="vod_segment", segment_id=seg)
                if sid in existing:
                    continue
                register_vod_label(
                    path=path,
                    segment_id=seg,
                    is_good=is_good,
                    row=row,
                    reason=str(row.get("reason") or ""),
                    archive_path=str(row.get("training_archive") or ""),
                    exemplar_path=str(row.get("exemplar") or ""),
                )
                existing.add(sid)
                added += 1

    return added
