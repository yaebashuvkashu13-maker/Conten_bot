#!/usr/bin/env python3
"""Build allowed parameter ranges from sent PUBG clips, then gate new clips.

User goal (plain):
  1) remember numeric params of every sent video
  2) from that database compute allowed ranges
  3) only ship future clips that fall inside those ranges

👍 goods tighten the target when enough exist; otherwise all `decision=sent` rows.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Any

# Flat numeric fields we expect on presend_report / ledger metrics.
RANGE_KEYS: tuple[str, ...] = (
    "duration",
    "gunfire_density",
    "burst_ratio",
    "audio_rms",
    "center_motion",
    "center_text",
    "panns_gunshot",
    "panns_machine_gun",
    "panns_gun_max",
    "panns_speech",
    "panns_music",
    "killfeed_density",
    "quality_score",
    "heuristic_score",
    "ranker_score",
)


def ranges_path(game: str = "pubg") -> Path:
    root = Path(os.environ.get("VOD_ADAPTIVE_THRESH_DIR", "/root/data/vod_adaptive_thresholds"))
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{game}_sent_ranges.json"


def ledger_path(game: str = "pubg") -> Path:
    from vod_clip_quality_ledger import ledger_path as _lp

    return _lp(game)


def segment_labels_path(game: str = "pubg") -> Path:
    return Path(
        os.environ.get(
            "PUBG_SEGMENT_LABELS_PATH",
            f"/root/data/{game}/vod_segment_labels.json",
        )
    )


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


def flatten_metrics(metrics: dict[str, Any] | None) -> dict[str, float]:
    """Pull RANGE_KEYS from a metrics/presend blob (nested-safe for a few aliases)."""
    if not isinstance(metrics, dict):
        return {}
    out: dict[str, float] = {}
    aliases = {
        "gunfire_density": ("gunfire_density", "gun_density"),
        "burst_ratio": ("burst_ratio", "gun_burst_ratio"),
        "audio_rms": ("audio_rms", "rms"),
        "center_motion": ("center_motion", "motion"),
        "center_text": ("center_text", "menu_overlay"),
        "duration": ("duration", "output_duration", "clip_duration"),
    }
    for key in RANGE_KEYS:
        names = aliases.get(key, (key,))
        for name in names:
            val = _as_float(metrics.get(name))
            if val is not None:
                out[key] = val
                break
        if key not in out:
            comps = metrics.get("components") if isinstance(metrics.get("components"), dict) else {}
            val = _as_float(comps.get(key))
            if val is not None:
                out[key] = val
    return out


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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
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


def collect_param_rows(game: str = "pubg") -> list[dict[str, Any]]:
    """One row per clip with flat params + optional owner label."""
    ledger = _read_jsonl(ledger_path(game))
    sent_by_id: dict[str, dict[str, Any]] = {}
    feedback_by_id: dict[str, str] = {}
    for row in ledger:
        decision = str(row.get("decision") or "")
        cid = str(row.get("clip_id") or "").strip()
        if not cid:
            continue
        if decision == "sent":
            flat = flatten_metrics(row.get("metrics") if isinstance(row.get("metrics"), dict) else {})
            if not flat:
                continue
            sent_by_id[cid] = {
                "clip_id": cid,
                "vod_id": row.get("vod_id"),
                "peak_sec": row.get("peak_sec"),
                "ts": row.get("ts"),
                "source": "ledger_sent",
                "params": flat,
            }
        elif decision == "feedback":
            lab = str(row.get("label") or "")
            if lab in {"good", "bad"}:
                feedback_by_id[cid] = lab

    for cid, lab in feedback_by_id.items():
        if cid in sent_by_id:
            sent_by_id[cid]["label"] = lab

    # Segment labels good/bad often carry quality_metrics even if ledger join missed.
    labels_path = segment_labels_path(game)
    if labels_path.is_file():
        try:
            labels = json.loads(labels_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            labels = {}
        for bucket, lab in (("good", "good"), ("bad", "bad")):
            for row in labels.get(bucket) or []:
                if not isinstance(row, dict):
                    continue
                cid = str(row.get("segment_id") or "").strip()
                flat = flatten_metrics(
                    row.get("quality_metrics") if isinstance(row.get("quality_metrics"), dict) else {}
                )
                if not cid or not flat:
                    continue
                if cid not in sent_by_id:
                    sent_by_id[cid] = {
                        "clip_id": cid,
                        "vod_id": row.get("vod_id") or row.get("vod"),
                        "peak_sec": row.get("peak_start") or row.get("start"),
                        "ts": row.get("at"),
                        "source": "segment_labels",
                        "params": flat,
                        "label": lab,
                    }
                else:
                    sent_by_id[cid]["label"] = lab
                    # Fill missing params from labels.
                    for k, v in flat.items():
                        sent_by_id[cid]["params"].setdefault(k, v)

    return list(sent_by_id.values())


def build_ranges(
    game: str = "pubg",
    *,
    low_q: float | None = None,
    high_q: float | None = None,
    min_samples: int | None = None,
    prefer_good: bool = True,
) -> dict[str, Any]:
    low_q = float(os.environ.get("PUBG_SENT_RANGE_LOW_Q", low_q if low_q is not None else 0.10))
    high_q = float(os.environ.get("PUBG_SENT_RANGE_HIGH_Q", high_q if high_q is not None else 0.90))
    min_samples = int(
        os.environ.get("PUBG_SENT_RANGE_MIN_SAMPLES", min_samples if min_samples is not None else 25)
    )

    rows = collect_param_rows(game)
    goods = [r for r in rows if r.get("label") == "good"]
    use = goods if (prefer_good and len(goods) >= max(8, min_samples // 3)) else rows
    cohort = "good" if use is goods and goods else "sent"

    columns: dict[str, list[float]] = {k: [] for k in RANGE_KEYS}
    for row in use:
        params = row.get("params") or {}
        for key in RANGE_KEYS:
            val = _as_float(params.get(key))
            if val is not None:
                columns[key].append(val)

    ranges: dict[str, dict[str, float]] = {}
    for key, vals in columns.items():
        if len(vals) < max(5, min_samples // 5):
            continue
        vals_sorted = sorted(vals)
        lo = _percentile(vals_sorted, low_q)
        hi = _percentile(vals_sorted, high_q)
        if hi < lo:
            lo, hi = hi, lo
        # Tiny padding so exact boundary clips still pass.
        pad = max(1e-6, (hi - lo) * 0.02)
        ranges[key] = {
            "min": lo - pad,
            "max": hi + pad,
            "p50": _percentile(vals_sorted, 0.50),
            "n": float(len(vals_sorted)),
        }

    payload = {
        "game": game,
        "cohort": cohort,
        "low_q": low_q,
        "high_q": high_q,
        "min_samples": min_samples,
        "rows_total": len(rows),
        "rows_used": len(use),
        "goods": len(goods),
        "ranges": ranges,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ready": len(ranges) >= 3 and len(use) >= min_samples,
    }
    path = ranges_path(game)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return payload


def load_ranges(game: str = "pubg") -> dict[str, Any]:
    path = ranges_path(game)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def evaluate_against_ranges(
    metrics: dict[str, Any] | None,
    *,
    game: str = "pubg",
    ranges_blob: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Return (ok, reason, report). Soft-pass if ranges not ready yet."""
    if os.environ.get("PUBG_SENT_RANGE_GATE", "1") != "1":
        return True, "range_gate_disabled", {"skipped": True}

    blob = ranges_blob if ranges_blob is not None else load_ranges(game)
    ranges = blob.get("ranges") if isinstance(blob.get("ranges"), dict) else {}
    if not blob.get("ready") or not ranges:
        return True, "range_gate_warming", {"skipped": True, "ready": False}

    flat = flatten_metrics(metrics)
    report: dict[str, Any] = {"checked": {}, "violations": []}
    max_violations = int(os.environ.get("PUBG_SENT_RANGE_MAX_VIOLATIONS", "1"))
    # Keys that must be present when ranges exist for them.
    required = [
        k
        for k in ("gunfire_density", "burst_ratio", "audio_rms", "duration")
        if k in ranges
    ]

    violations = 0
    for key, bounds in ranges.items():
        val = flat.get(key)
        if val is None:
            if key in required:
                violations += 1
                report["violations"].append({"key": key, "reason": "missing"})
            continue
        lo = float(bounds.get("min", float("-inf")))
        hi = float(bounds.get("max", float("inf")))
        report["checked"][key] = {"value": val, "min": lo, "max": hi}
        if val < lo or val > hi:
            violations += 1
            report["violations"].append(
                {"key": key, "value": val, "min": lo, "max": hi}
            )

    report["violation_count"] = violations
    if violations > max_violations:
        first = report["violations"][0]
        reason = (
            f"out_of_sent_range:{first.get('key')}="
            f"{first.get('value', 'missing')}"
        )
        return False, reason, report
    return True, "in_sent_range", report


def main() -> int:
    parser = argparse.ArgumentParser(description="PUBG sent-param ranges")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="Rebuild ranges from ledger/labels")
    b.add_argument("--game", default="pubg")
    b.add_argument("--low-q", type=float, default=None)
    b.add_argument("--high-q", type=float, default=None)
    s = sub.add_parser("stats", help="Show current ranges file")
    s.add_argument("--game", default="pubg")
    e = sub.add_parser("eval", help="Eval one metrics JSON against ranges")
    e.add_argument("--game", default="pubg")
    e.add_argument("--metrics-json", required=True)
    args = parser.parse_args()

    if args.cmd == "build":
        payload = build_ranges(args.game, low_q=args.low_q, high_q=args.high_q)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("ready") or payload.get("rows_used", 0) > 0 else 2
    if args.cmd == "stats":
        print(json.dumps(load_ranges(args.game), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "eval":
        metrics = json.loads(args.metrics_json)
        ok, reason, report = evaluate_against_ranges(metrics, game=args.game)
        print(json.dumps({"ok": ok, "reason": reason, "report": report}, ensure_ascii=False))
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
