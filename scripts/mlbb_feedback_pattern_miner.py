#!/usr/bin/env python3
"""Mine 👍/👎 feedback patterns and emit gate/rank recommendations for MLBB VOD."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

NUMERIC_KEYS = (
    "score",
    "hook_score",
    "clip_score",
    "fight_dur",
    "duration",
    "peak_start",
    "start",
    "kill_banner_tier",
)


def _data_root() -> Path:
    return Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))


def _repo_root() -> Path:
    env = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent.parent
    return root if (root / "data").exists() else Path("/root/content_bot_ml")


def patterns_path() -> Path:
    return Path(os.environ.get("MLBB_FEEDBACK_PATTERNS_PATH", str(_data_root() / "feedback_patterns.json")))


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _percentile(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    ordered = sorted(vals)
    idx = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * q))))
    return float(ordered[idx])


def _feature_stats(rows: list[dict], key: str) -> dict[str, float | int] | None:
    vals = [float(r.get(key) or 0) for r in rows if r.get(key) is not None]
    if not vals:
        return None
    return {
        "n": len(vals),
        "mean": round(statistics.mean(vals), 4),
        "p25": round(_percentile(vals, 0.25), 4),
        "p50": round(statistics.median(vals), 4),
        "p75": round(_percentile(vals, 0.75), 4),
    }


def _load_vod_labels() -> dict[str, list[dict]]:
    path = Path(os.environ.get("MLBB_VOD_SEGMENT_LABELS", str(_data_root() / "vod_segment_labels.json")))
    data = _read_json(path, {"good": [], "bad": [], "feedback": []})
    return {
        "good": list(data.get("good") or []),
        "bad": list(data.get("bad") or []),
        "feedback": list(data.get("feedback") or []),
    }


def _load_segment_index() -> dict[str, dict]:
    path = Path(os.environ.get("MLBB_VOD_SEGMENT_INDEX", str(_data_root() / "vod_segment_index.json")))
    data = _read_json(path, {"segments": []})
    out: dict[str, dict] = {}
    for row in data.get("segments") or []:
        if isinstance(row, dict) and row.get("segment_id"):
            out[str(row["segment_id"])] = row
    return out


def _load_calibration_labels() -> dict[str, list[dict]]:
    for candidate in (
        Path(os.environ.get("MLBB_CALIBRATION_LABELS", str(_data_root() / "calibration_labels.json"))),
        _repo_root() / "data" / "mlbb" / "calibration_labels.json",
    ):
        data = _read_json(candidate, {})
        if data.get("good") or data.get("bad"):
            return {"good": list(data.get("good") or []), "bad": list(data.get("bad") or [])}
    return {"good": [], "bad": []}


def _enrich(rows: list[dict], index: dict[str, dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        sid = str(row.get("segment_id") or "")
        merged = dict(row)
        extra = index.get(sid) or {}
        for key, val in extra.items():
            if key not in merged or merged.get(key) in (None, "", 0):
                merged[key] = val
        out.append(merged)
    return out


def _normalize_reason(reason: str) -> str:
    blob = str(reason or "").strip().lower()
    if not blob or blob in ("—", "-"):
        return "unknown"
    if blob == "button_dislike":
        return "unspecified"
    if blob.startswith("not_mlbb:"):
        return "not_mlbb"
    if blob.startswith("static_"):
        return "static"
    return blob.split(":")[0][:48]


def _reason_feature_table(rows: list[dict]) -> dict[str, dict[str, dict]]:
    by_reason: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_reason[_normalize_reason(str(row.get("reason") or ""))].append(row)
    out: dict[str, dict[str, dict]] = {}
    for reason, grp in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        feats: dict[str, dict] = {}
        for key in NUMERIC_KEYS:
            st = _feature_stats(grp, key)
            if st:
                feats[key] = st
        out[reason] = {"count": len(grp), "features": feats}
    return out


def _correlations(good: list[dict], bad: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for key in ("hook_score", "clip_score", "fight_dur", "score"):
        gst = _feature_stats(good, key)
        bst = _feature_stats(bad, key)
        if not gst or not bst:
            continue
        gap = float(gst["p50"]) - float(bst["p50"])
        rows.append(
            {
                "feature": key,
                "good_p50": gst["p50"],
                "bad_p50": bst["p50"],
                "gap": round(gap, 4),
                "direction": "higher_is_good" if gap >= 0 else "lower_is_good",
            }
        )
    rows.sort(key=lambda r: abs(float(r["gap"])), reverse=True)
    return rows


def _recommend_gates(good: list[dict], bad: list[dict], bad_by_reason: dict[str, dict]) -> dict[str, float]:
    g_hook = _feature_stats(good, "hook_score") or {}
    b_hook = _feature_stats(bad, "hook_score") or {}
    boring = (bad_by_reason.get("boring") or {}).get("features", {}).get("hook_score", {})
    g_fight = _feature_stats(good, "fight_dur") or {}
    b_fight = _feature_stats(bad, "fight_dur") or {}
    g_clip = _feature_stats(good, "clip_score") or {}
    b_clip = _feature_stats(bad, "clip_score") or {}

    hook_min = 0.08
    if boring:
        hook_min = max(hook_min, min(0.18, float(boring.get("p75") or 0) + 0.03))
    if g_hook:
        hook_min = max(hook_min, min(float(g_hook.get("p25") or 0) * 0.5, float(g_hook.get("p50") or 0) * 0.35))

    fight_min = 32.0
    if g_fight and b_fight:
        fight_min = max(28.0, min(45.0, (float(g_fight.get("p25") or 32) + float(b_fight.get("p75") or 32)) / 2))

    clip_min = 0.12
    if g_clip and b_clip:
        clip_min = max(0.10, min(0.22, float(g_clip.get("p25") or 0.12)))

    return {
        "VIRAL_MLBB_HOOK_MIN": round(hook_min, 3),
        "MLBB_VOD_MIN_CLIP_SCORE": round(clip_min, 3),
        "MLBB_FEEDBACK_MIN_FIGHT_DUR": round(fight_min, 1),
        "MLBB_FEEDBACK_REJECT_HOOK_BELOW": round(max(0.06, hook_min * 0.75), 3),
        "MLBB_FEEDBACK_REJECT_FIGHT_DUR_BELOW": round(max(24.0, fight_min - 8.0), 1),
    }


def _rank_profile(good: list[dict]) -> dict[str, float]:
    g_hook = _feature_stats(good, "hook_score") or {}
    g_fight = _feature_stats(good, "fight_dur") or {}
    g_clip = _feature_stats(good, "clip_score") or {}
    return {
        "hook_target": float(g_hook.get("p50") or 0.27),
        "fight_dur_target": float(g_fight.get("p50") or 48.0),
        "clip_target": float(g_clip.get("p50") or 0.25),
        "hook_weight": 0.45,
        "fight_dur_weight": 0.25,
        "clip_weight": 0.30,
    }


def mine_patterns() -> dict[str, Any]:
    vod = _load_vod_labels()
    index = _load_segment_index()
    cal = _load_calibration_labels()

    good = _enrich(vod["good"], index)
    bad = _enrich(vod["bad"], index)
    yes = sum(1 for f in vod["feedback"] if f.get("owner_label") in ("yes", "good"))
    no = sum(1 for f in vod["feedback"] if f.get("owner_label") in ("no", "bad"))
    rated = yes + no
    precision = yes / rated if rated else 0.0

    bad_reasons = Counter(_normalize_reason(str(b.get("reason") or "")) for b in bad)
    cal_bad_reasons = Counter(_normalize_reason(str(b.get("reason") or b.get("label") or "")) for b in cal["bad"])

    features = {
        "good": {k: v for k in NUMERIC_KEYS if (v := _feature_stats(good, k))},
        "bad": {k: v for k in NUMERIC_KEYS if (v := _feature_stats(bad, k))},
    }
    bad_by_reason = _reason_feature_table(bad)
    correlations = _correlations(good, bad)
    gates = _recommend_gates(good, bad, bad_by_reason)
    rank = _rank_profile(good)

    return {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sources": {
            "vod_good": len(good),
            "vod_bad": len(bad),
            "vod_feedback": len(vod["feedback"]),
            "cal_good": len(cal["good"]),
            "cal_bad": len(cal["bad"]),
            "index_segments": len(index),
        },
        "precision": round(precision, 4),
        "bad_share": round(1.0 - precision, 4) if rated else 1.0,
        "rated": rated,
        "bad_reasons_vod": bad_reasons.most_common(12),
        "bad_reasons_shorts": cal_bad_reasons.most_common(12),
        "features": features,
        "bad_by_reason": bad_by_reason,
        "correlations": correlations,
        "gates": gates,
        "rank_profile": rank,
        "insights": _insights(correlations, bad_by_reason, bad_reasons),
    }


def _insights(correlations: list[dict], bad_by_reason: dict, bad_reasons: Counter) -> list[str]:
    lines: list[str] = []
    top_reason = bad_reasons.most_common(1)
    if top_reason:
        lines.append(f"Top VOD 👎 reason: {top_reason[0][0]} ({top_reason[0][1]}x)")
    for row in correlations[:3]:
        lines.append(
            f"{row['feature']}: good p50={row['good_p50']} vs bad p50={row['bad_p50']} (gap {row['gap']:+.3f})"
        )
    boring = bad_by_reason.get("boring")
    if boring:
        hook = boring.get("features", {}).get("hook_score", {})
        if hook:
            lines.append(f"boring 👎 hook p50={hook.get('p50')} — low hook strongly correlates with boring")
    return lines


def save_patterns(payload: dict[str, Any] | None = None) -> Path:
    payload = payload if payload is not None else mine_patterns()
    path = patterns_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def format_report(payload: dict[str, Any]) -> str:
    lines = [
        "📊 MLBB feedback patterns",
        f"VOD: 👍{payload['sources']['vod_good']} 👎{payload['sources']['vod_bad']} "
        f"precision={payload['precision']:.0%} bad_share={payload['bad_share']:.0%}",
        "",
        "Топ 👎 VOD:",
    ]
    for reason, count in payload.get("bad_reasons_vod", [])[:5]:
        lines.append(f"  • {reason}: {count}")
    lines.extend(["", "Корреляции 👍 vs 👎:"])
    for row in payload.get("correlations", [])[:4]:
        lines.append(
            f"  • {row['feature']}: good {row['good_p50']} / bad {row['bad_p50']} (Δ {row['gap']:+.3f})"
        )
    lines.extend(["", "Рекомендованные гейты:"])
    for key, val in payload.get("gates", {}).items():
        lines.append(f"  • {key}={val}")
    lines.extend(["", "Инсайты:"])
    for row in payload.get("insights", [])[:5]:
        lines.append(f"  • {row}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mine MLBB owner feedback patterns")
    parser.add_argument("--write", action="store_true", help="Write feedback_patterns.json")
    parser.add_argument("--print", action="store_true", help="Print human report")
    args = parser.parse_args()
    payload = mine_patterns()
    if args.write or not args.print:
        path = save_patterns(payload)
        print(json.dumps({"path": str(path), "precision": payload["precision"], "gates": payload["gates"]}, ensure_ascii=False))
    if args.print:
        print(format_report(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
