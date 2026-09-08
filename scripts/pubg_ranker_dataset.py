#!/usr/bin/env python3
"""Versioned PUBG ranker dataset: export → train from JSONL → grow with window labels.

Schema (one JSON object per line):
  video_id, peak_sec, t0, t1, label (0|1), source, weight, event?, features?,
  video_path?, labeled_at?
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

DATASET_VERSION = 1


def default_dataset_path() -> Path:
    return Path(
        os.environ.get(
            "PUBG_RANKER_DATASET",
            "/root/data/pubg/ranker_dataset/moments_v1.jsonl",
        )
    )


def window_labels_path() -> Path:
    return Path(
        os.environ.get(
            "PUBG_WINDOW_LABELS_PATH",
            "/root/data/pubg/window_labels.jsonl",
        )
    )


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            n += 1
    return n


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
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


def load_window_label_rows(path: Path | None = None) -> list[dict[str, Any]]:
    """Normalize window_labels.jsonl into ranker sample dicts."""
    out: list[dict[str, Any]] = []
    for row in load_jsonl(path or window_labels_path()):
        try:
            label_raw = row.get("label")
            if label_raw in (1, "1", "good", "yes", True):
                label = 1
            elif label_raw in (0, "0", "bad", "no", False):
                label = 0
            else:
                continue
            peak = float(row.get("peak_sec", row.get("t0", 0)) or 0)
            t0 = float(row.get("t0", max(0.0, peak - 7.0)))
            t1 = float(row.get("t1", t0 + 15.0))
            video_id = str(row.get("video_id") or "").strip()
            if not video_id:
                continue
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "dataset_version": DATASET_VERSION,
                "video_id": video_id,
                "peak_sec": peak,
                "t0": t0,
                "t1": t1,
                "label": label,
                "source": str(row.get("source") or "window_label"),
                "weight": float(row.get("weight") or 1.5),
                "event": row.get("event"),
                "features": row.get("features"),
                "video_path": row.get("video_path"),
                "labeled_at": row.get("labeled_at"),
            }
        )
    return out


def export_dataset(
    *,
    output: Path | None = None,
    include_windows: bool = True,
    extract_missing_features: bool = False,
) -> dict[str, Any]:
    """Materialize a reproducible JSONL from owner labels + feedback + windows."""
    import pubg_moment_ranker as ranker

    output = output or default_dataset_path()
    samples = list(ranker.load_training_samples())
    rows: list[dict[str, Any]] = []

    for sample in samples:
        features = sample.features
        if features is None and extract_missing_features:
            try:
                features = ranker.extract_features(
                    sample.video_path,
                    max(0.0, sample.peak_sec - 7.0),
                    14.0,
                )
            except Exception:
                features = None
        rows.append(
            {
                "dataset_version": DATASET_VERSION,
                "video_id": sample.video_id,
                "peak_sec": round(float(sample.peak_sec), 2),
                "t0": round(max(0.0, float(sample.peak_sec) - 7.0), 2),
                "t1": round(max(0.0, float(sample.peak_sec) - 7.0) + 14.0, 2),
                "label": int(sample.label),
                "source": sample.source,
                "weight": float(sample.weight),
                "event": None,
                "features": features,
                "video_path": str(sample.video_path),
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )

    if include_windows:
        rows.extend(load_window_label_rows())

    # Last write wins for same (video_id, rounded peak).
    dedup: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["video_id"]), round(float(row["peak_sec"])))
        dedup[key] = row
    final_rows = sorted(dedup.values(), key=lambda r: (r["video_id"], r["peak_sec"]))

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in final_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, output)

    positives = sum(1 for r in final_rows if int(r["label"]) == 1)
    return {
        "status": "exported",
        "path": str(output),
        "rows": len(final_rows),
        "positive": positives,
        "negative": len(final_rows) - positives,
        "dataset_version": DATASET_VERSION,
        "with_features": sum(1 for r in final_rows if r.get("features")),
    }


def append_window_label(
    *,
    video_id: str,
    t0: float,
    t1: float,
    label: str | int | bool,
    event: str = "",
    video_path: str = "",
    source: str = "window_label",
    path: Path | None = None,
) -> dict[str, Any]:
    """Record one owner rating on a fixed window (the learnable-loop unit)."""
    if label in (1, "1", "good", "yes", True):
        lab = "good"
        lab_i = 1
    elif label in (0, "0", "bad", "no", False):
        lab = "bad"
        lab_i = 0
    else:
        raise ValueError(f"unsupported label={label!r}")
    t0_f = max(0.0, float(t0))
    t1_f = max(t0_f + 1.0, float(t1))
    peak = (t0_f + t1_f) * 0.5
    row = {
        "dataset_version": DATASET_VERSION,
        "video_id": str(video_id).strip(),
        "t0": round(t0_f, 2),
        "t1": round(t1_f, 2),
        "peak_sec": round(peak, 2),
        "label": lab,
        "label_int": lab_i,
        "event": (event or "").strip()[:64] or None,
        "source": source,
        "weight": 1.5,
        "video_path": video_path or None,
        "labeled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out = path or window_labels_path()
    _append_jsonl(out, [row])

    # Mirror into owner timestamps so existing train path also sees it.
    try:
        from vod_owner_learning import append_owner_time_label

        append_owner_time_label(
            "pubg",
            row["video_id"],
            peak,
            lab,
            note=f"window {t0_f:.0f}-{t1_f:.0f}" + (f" event={event}" if event else ""),
            source=source,
        )
    except Exception:
        pass
    return row


def samples_from_dataset(path: Path) -> list[Any]:
    """Build TrainingSample list from an exported JSONL (no VOD required if features set)."""
    import pubg_moment_ranker as ranker

    rows = load_jsonl(path)
    samples: list[ranker.TrainingSample] = []
    for row in rows:
        try:
            video_id = str(row["video_id"])
            peak = float(row["peak_sec"])
            label = int(row["label"])
            weight = float(row.get("weight") or 1.0)
            source = str(row.get("source") or "dataset")
        except (KeyError, TypeError, ValueError):
            continue
        hinted = str(row.get("video_path") or "")
        vod = ranker.resolve_vod(video_id, hinted_path=hinted)
        if vod is None and not row.get("features"):
            continue
        if vod is None:
            # Synthetic path placeholder — train uses cached features only.
            vod = Path(hinted or f"/tmp/missing_yt_{video_id}.mp4")
        features = row.get("features")
        if isinstance(features, dict):
            features = {name: float(features.get(name, 0.0) or 0.0) for name in ranker.FEATURE_NAMES}
        else:
            features = None
        samples.append(
            ranker.TrainingSample(
                video_id,
                vod,
                peak,
                label,
                source,
                weight,
                features,
            )
        )
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description="PUBG ranker dataset export / window labels")
    sub = parser.add_subparsers(dest="cmd", required=True)

    export_p = sub.add_parser("export", help="Export versioned moments JSONL")
    export_p.add_argument("--output", type=Path, default=None)
    export_p.add_argument("--extract-features", action="store_true")
    export_p.add_argument("--no-windows", action="store_true")

    add_p = sub.add_parser("add-window", help="Append one window label")
    add_p.add_argument("--video-id", required=True)
    add_p.add_argument("--t0", type=float, required=True)
    add_p.add_argument("--t1", type=float, required=True)
    add_p.add_argument("--label", required=True, choices=("good", "bad", "1", "0"))
    add_p.add_argument("--event", default="")
    add_p.add_argument("--video-path", default="")

    stats_p = sub.add_parser("stats", help="Count dataset / window labels")
    stats_p.add_argument("--dataset", type=Path, default=None)

    args = parser.parse_args()
    if args.cmd == "export":
        report = export_dataset(
            output=args.output,
            include_windows=not args.no_windows,
            extract_missing_features=args.extract_features,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    if args.cmd == "add-window":
        row = append_window_label(
            video_id=args.video_id,
            t0=args.t0,
            t1=args.t1,
            label=args.label,
            event=args.event,
            video_path=args.video_path,
        )
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        return 0
    if args.cmd == "stats":
        ds = args.dataset or default_dataset_path()
        drows = load_jsonl(ds)
        wrows = load_jsonl(window_labels_path())
        print(
            json.dumps(
                {
                    "dataset": str(ds),
                    "dataset_rows": len(drows),
                    "dataset_good": sum(1 for r in drows if int(r.get("label", 0) or 0) == 1),
                    "window_labels": str(window_labels_path()),
                    "window_rows": len(wrows),
                    "window_good": sum(
                        1
                        for r in wrows
                        if r.get("label") in (1, "1", "good", "yes", True)
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
