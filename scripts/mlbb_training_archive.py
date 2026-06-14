#!/usr/bin/env python3
"""Owner training archive — full clips kept for reuse (by year)."""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path


def archive_enabled() -> bool:
    return os.environ.get("MLBB_TRAINING_ARCHIVE", "1") == "1"


def archive_root() -> Path:
    return Path(
        os.environ.get("MLBB_TRAINING_ARCHIVE_ROOT", "/root/datasets/mlbb/training_archive")
    )


def archive_index_path() -> Path:
    data = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    return Path(os.environ.get("MLBB_TRAINING_ARCHIVE_INDEX", str(data / "training_archive_index.jsonl")))


def archive_year(*, upload_date: str = "") -> str:
    forced = str(os.environ.get("MLBB_TRAINING_ARCHIVE_YEAR", "")).strip()
    if forced.isdigit() and len(forced) == 4:
        return forced
    if upload_date and len(upload_date) >= 4 and upload_date[:4].isdigit():
        return upload_date[:4]
    return str(datetime.now().year)


def archive_for_reuse(
    src: Path,
    dest_name: str,
    *,
    kind: str,
    video_id: str = "",
    upload_date: str = "",
    extra: dict | None = None,
) -> Path | None:
    """Copy source mp4 into year/kind/ for owner reuse."""
    if not archive_enabled() or not src.exists():
        return None
    year = archive_year(upload_date=upload_date)
    dest_dir = archive_root() / year / kind
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / dest_name
    if not dest.exists():
        try:
            shutil.copy2(src, dest)
        except OSError:
            return None
    row = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "year": year,
        "kind": kind,
        "video_id": video_id,
        "upload_date": upload_date,
        "path": str(dest),
        "source": str(src),
        **(extra or {}),
    }
    idx = archive_index_path()
    idx.parent.mkdir(parents=True, exist_ok=True)
    with idx.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return dest


def archive_short(src: Path, video_id: str, *, upload_date: str = "", title: str = "") -> Path | None:
    vid = video_id.strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    return archive_for_reuse(
        src,
        f"yt_{vid}.mp4",
        kind="shorts",
        video_id=vid,
        upload_date=upload_date,
        extra={"title": title[:240]} if title else None,
    )


def archive_vod_segment(src: Path, segment_id: str, *, vod_id: str = "", peak_sec: float = 0.0) -> Path | None:
    sid = segment_id.strip()
    return archive_for_reuse(
        src,
        f"seg_{sid}.mp4",
        kind="vod_segments",
        video_id=vod_id or sid.rsplit("_", 1)[0],
        extra={"segment_id": sid, "peak_sec": peak_sec},
    )
