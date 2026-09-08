#!/usr/bin/env python3
"""PUBG Shorts autolearn feature table + tightening parameter ranges.

Stores every watched Short as a flat row of VOD-cutting params, then builds
percentile envelopes that start wide and tighten as N grows.
"""

from __future__ import annotations

import csv
import json
import math
import os
import time
from pathlib import Path
from typing import Any

from pubg_sent_param_ranges import RANGE_KEYS, flatten_metrics


def _as_float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return None
        return out
    except (TypeError, ValueError):
        return None


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    q = min(1.0, max(0.0, float(q)))
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = pos - lo
    return float(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)

# Extra columns useful for VOD cutting / owner reports (beyond RANGE_KEYS).
EXTRA_KEYS: tuple[str, ...] = (
    "view_count",
    "hook_score",
    "clip_score",
    "fight_score",
    "payoff_score",
    "kill_notification_score",
    "has_author_kill",
    "loot_walk",
    "panns_explosion",
)

TABLE_COLUMNS: tuple[str, ...] = (
    "ts",
    "video_id",
    "title",
    "source",
    "source_url",
    "search_query",
    "path",
    "duration",
    *RANGE_KEYS,
    *EXTRA_KEYS,
    "quality_ok",
    "reject_reason",
    "batch_id",
)


def data_root() -> Path:
    return Path(
        os.environ.get(
            "PUBG_SHORTS_AUTOLEARN_DIR",
            "/root/data/pubg/shorts_autolearn",
        )
    )


def features_jsonl_path() -> Path:
    return data_root() / "features.jsonl"


def features_csv_path() -> Path:
    return data_root() / "features.csv"


def ranges_path() -> Path:
    return data_root() / "silver_ranges.json"


def state_path() -> Path:
    return data_root() / "state.json"


def reports_dir() -> Path:
    d = data_root() / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.is_file():
        return {
            "watched_total": 0,
            "last_report_at_count": 0,
            "last_probe_at_count": 0,
            "seen_ids": [],
            "batches": 0,
            "last_error": "",
            "updated_at": "",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Cap seen_ids growth.
    seen = list(state.get("seen_ids") or [])
    if len(seen) > 5000:
        state["seen_ids"] = seen[-5000:]
    tmp = state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, state_path())


def iter_feature_rows() -> list[dict[str, Any]]:
    path = features_jsonl_path()
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def append_feature_row(row: dict[str, Any]) -> None:
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    path = features_jsonl_path()
    lock_path = root / ".write.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        try:
            import fcntl

            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        rebuild_csv()
        try:
            import fcntl

            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass


def rebuild_csv() -> Path:
    rows = iter_feature_rows()
    path = features_csv_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(TABLE_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flat = {k: row.get(k, "") for k in TABLE_COLUMNS}
            writer.writerow(flat)
    return path


def quantile_schedule(n: int) -> tuple[float, float, str]:
    """Wider early, tighter later — matches owner request."""
    if n < 100:
        return 0.05, 0.95, "warm_wide"
    if n < 300:
        return 0.10, 0.90, "forming"
    if n < 600:
        return 0.15, 0.85, "tightening"
    return 0.20, 0.80, "minimal"


def build_silver_ranges(
    *,
    rows: list[dict[str, Any]] | None = None,
    only_quality_ok: bool = True,
) -> dict[str, Any]:
    rows = list(rows if rows is not None else iter_feature_rows())
    if only_quality_ok:
        use = [r for r in rows if r.get("quality_ok") in (True, 1, "1", "true")]
        if len(use) < max(10, len(rows) // 5):
            use = rows
    else:
        use = rows
    n = len(use)
    low_q, high_q, stage = quantile_schedule(n)
    columns: dict[str, list[float]] = {k: [] for k in RANGE_KEYS}
    for row in use:
        for key in RANGE_KEYS:
            val = _as_float(row.get(key))
            if val is not None:
                columns[key].append(val)

    ranges: dict[str, dict[str, float]] = {}
    min_col = max(5, min(20, n // 5) if n else 5)
    for key, vals in columns.items():
        if len(vals) < min_col:
            continue
        vals_sorted = sorted(vals)
        lo = _percentile(vals_sorted, low_q)
        hi = _percentile(vals_sorted, high_q)
        if hi < lo:
            lo, hi = hi, lo
        pad = max(1e-6, (hi - lo) * 0.02)
        ranges[key] = {
            "min": lo - pad,
            "max": hi + pad,
            "p50": _percentile(vals_sorted, 0.50),
            "n": float(len(vals_sorted)),
        }

    payload = {
        "game": "pubg",
        "source": "shorts_silver",
        "stage": stage,
        "low_q": low_q,
        "high_q": high_q,
        "rows_total": len(rows),
        "rows_used": n,
        "ranges": ranges,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ready": len(ranges) >= 3 and n >= int(os.environ.get("PUBG_SHORTS_RANGE_MIN", "25")),
    }
    path = ranges_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return payload


def format_ranges_table(ranges_blob: dict[str, Any], *, limit: int = 12) -> str:
    ranges = ranges_blob.get("ranges") if isinstance(ranges_blob.get("ranges"), dict) else {}
    if not ranges:
        return "(диапазоны ещё пустые — мало данных)"
    lines = [
        f"stage={ranges_blob.get('stage')} q={ranges_blob.get('low_q'):.2f}-{ranges_blob.get('high_q'):.2f} "
        f"n={ranges_blob.get('rows_used')}/{ranges_blob.get('rows_total')} ready={ranges_blob.get('ready')}"
    ]
    for i, (key, band) in enumerate(ranges.items()):
        if i >= limit:
            lines.append(f"… +{len(ranges) - limit} keys")
            break
        if not isinstance(band, dict):
            continue
        lines.append(
            f"• {key}: [{band.get('min'):.4g} .. {band.get('max'):.4g}] "
            f"p50={band.get('p50'):.4g} (n={int(band.get('n') or 0)})"
        )
    return "\n".join(lines)


def row_from_short_metrics(
    *,
    video_id: str,
    title: str,
    source_url: str,
    search_query: str,
    path: str,
    meta: dict[str, Any],
    quality_report: dict[str, Any],
    quality_ok: bool,
    reject_reason: str,
    batch_id: int,
    source: str = "youtube_shorts",
) -> dict[str, Any]:
    flat = flatten_metrics(quality_report)
    # Fill duration from report/meta/file probe fields.
    if "duration" not in flat:
        dur = _as_float(meta.get("duration")) or _as_float(quality_report.get("duration"))
        if dur is not None:
            flat["duration"] = dur
    row: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "video_id": video_id,
        "title": (title or "")[:200],
        "source": source,
        "source_url": source_url,
        "search_query": search_query,
        "path": path,
        "batch_id": batch_id,
        "quality_ok": bool(quality_ok),
        "reject_reason": (reject_reason or "")[:160],
        "view_count": int(meta.get("view_count") or 0),
        **flat,
    }
    for key in EXTRA_KEYS:
        if key in row:
            continue
        val = quality_report.get(key)
        if isinstance(val, bool):
            row[key] = int(val)
        elif isinstance(val, (int, float)) and not isinstance(val, bool):
            if not (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
                row[key] = float(val)
    author = quality_report.get("author") if isinstance(quality_report.get("author"), dict) else {}
    if "has_author_kill" not in row and author:
        row["has_author_kill"] = int(bool(author.get("has_author_kill")))
    if "loot_walk" not in row and "loot_walk" in quality_report:
        row["loot_walk"] = int(bool(quality_report.get("loot_walk")))
    return row
