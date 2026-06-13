#!/usr/bin/env python3
"""LEARNING_FIRST mode: no sendVideo until gate passes; metric = precision_7d."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

DATA_MLBB = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))


def _state_path() -> Path:
    return Path(os.environ.get("MLBB_LEARNING_STATE", str(DATA_MLBB / "learning_first_state.json")))


def _gate_report_path() -> Path:
    return Path(os.environ.get("MLBB_LEARNING_GATE_REPORT", str(DATA_MLBB / "learning_first_gate.json")))


def _vseg_labels_path() -> Path:
    return Path(os.environ.get("MLBB_VOD_SEGMENT_LABELS", str(DATA_MLBB / "vod_segment_labels.json")))


INBOX = Path(os.environ.get("HIGHLIGHT_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
PROFILE = "mobile_legends"

REQUIRED_BAD_CASES = (
    ("qa2iNyoPO2Q", 508.0, "qa2iNyoPO2Q_508"),
)


def enabled() -> bool:
    return os.environ.get("MLBB_LEARNING_FIRST", "0") == "1"


def load_state() -> dict:
    if not _state_path().exists():
        return {"transition_passed": False, "daily_sends": {}, "last_gate": {}}
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"transition_passed": False, "daily_sends": {}, "last_gate": {}}
    data.setdefault("transition_passed", False)
    data.setdefault("daily_sends", {})
    data.setdefault("last_gate", {})
    return data


def save_state(state: dict) -> None:
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    _state_path().write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def transition_passed() -> bool:
    if not enabled():
        return True
    return bool(load_state().get("transition_passed"))


def set_transition_passed(value: bool = True) -> None:
    state = load_state()
    state["transition_passed"] = value
    state["transition_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)


def sends_allowed() -> bool:
    if not enabled():
        return True
    return transition_passed()


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def daily_send_count() -> int:
    state = load_state()
    return int(state.get("daily_sends", {}).get(_today_key(), 0))


def record_send(count: int = 1) -> None:
    state = load_state()
    daily = state.setdefault("daily_sends", {})
    daily[_today_key()] = int(daily.get(_today_key(), 0)) + count
    save_state(state)


def max_daily_sends() -> int:
    prec = precision_7d()
    target = float(os.environ.get("MLBB_PRECISION_TARGET", "0.45"))
    if prec >= target:
        return int(os.environ.get("MLBB_MAX_DAILY_SENDS", "150"))
    return int(os.environ.get("MLBB_LEARNING_MAX_DAILY", "150"))


def can_send(count: int = 1) -> tuple[bool, str]:
    if enabled() and not sends_allowed():
        return False, "learning_first_gate"
    cap = max_daily_sends()
    if daily_send_count() + count > cap:
        return False, f"daily_cap_{cap}_precision7d_{precision_7d():.2f}"
    return True, "ok"


def _parse_at(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def precision_7d() -> float:
    """Owner 👍/(👍+👎) on VOD segments in the last 7 days."""
    if not _vseg_labels_path().exists():
        return 0.0
    try:
        data = json.loads(_vseg_labels_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0.0
    cutoff = datetime.now() - timedelta(days=7)
    yes = no = 0
    for row in data.get("feedback", []):
        at = _parse_at(str(row.get("at", "")))
        if at is None or at < cutoff:
            continue
        label = row.get("owner_label")
        if label in ("yes", "good"):
            yes += 1
        elif label in ("no", "bad"):
            no += 1
    total = yes + no
    return yes / total if total else float(load_state().get("last_precision_7d", 0.0))


def resolve_vod(video_id: str) -> Path | None:
    for candidate in (
        INBOX / f"yt_{video_id}.mp4",
        Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml")) / "data" / "samples" / f"yt_{video_id}.mp4",
    ):
        if candidate.exists():
            return candidate
    return None


def _bad_pad_sec() -> float:
    return float(os.environ.get("HIGHLIGHT_OWNER_BAD_PAD_SEC", "90"))


def _segment_vod_id(row: dict) -> str:
    vod_field = str(row.get("vod", "")).strip()
    if vod_field:
        from mlbb_vod_segment_store import vod_youtube_id

        return vod_youtube_id(Path(vod_field))
    sid = str(row.get("segment_id", ""))
    if "_" in sid:
        return sid.rsplit("_", 1)[0]
    return sid[:11]


def _extra_bad_cases() -> list[tuple[str, float, str]]:
    """Two additional bad labels from vod_segment_labels (distinct VODs, file on disk)."""
    if not _vseg_labels_path().exists():
        return []
    try:
        data = json.loads(_vseg_labels_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    seen_vods = {case[0] for case in REQUIRED_BAD_CASES}
    out: list[tuple[str, float, str]] = []
    for row in data.get("bad", []):
        sid = str(row.get("segment_id", ""))
        if not sid or sid == "qa2iNyoPO2Q_508":
            continue
        vid = _segment_vod_id(row)
        if len(vid) != 11 or vid in seen_vods:
            continue
        if resolve_vod(vid) is None:
            continue
        t_sec = float(row.get("start") or (sid.rsplit("_", 1)[-1] if "_" in sid else 0))
        out.append((vid, t_sec, sid))
        seen_vods.add(vid)
        if len(out) >= 2:
            break
    return out


def eval_bad_block_tests() -> dict:
    from highlight_scorer import _filter_bad_label_starts, segment_overlaps_owner_label

    pad = _bad_pad_sec()
    cases = list(REQUIRED_BAD_CASES) + _extra_bad_cases()
    results: list[dict] = []
    all_ok = True
    for vid, time_sec, sid in cases:
        vod = resolve_vod(vid)
        row: dict = {"segment_id": sid, "video_id": vid, "time_sec": time_sec, "pad_sec": pad}
        if not vod:
            row.update({"ok": False, "reason": "vod_missing"})
            all_ok = False
            results.append(row)
            continue
        win_start = max(0.0, time_sec - 5.0)
        overlap = segment_overlaps_owner_label(
            vod, win_start, 15.0, PROFILE, label="bad", pad_sec=pad
        )
        filtered = _filter_bad_label_starts(vod, PROFILE, [win_start, time_sec, time_sec + 30], pad_sec=pad)
        blocked_starts = [s for s in (win_start, time_sec, time_sec + 30) if s not in filtered]
        ok = overlap and len(blocked_starts) >= 2
        row.update(
            {
                "ok": ok,
                "overlap": overlap,
                "blocked_starts": len(blocked_starts),
                "vod": str(vod),
            }
        )
        if not ok:
            all_ok = False
            row["reason"] = "bad_not_blocked"
        results.append(row)
    return {"pass": all_ok and len(results) >= 3, "cases": results, "required": 3}


def _holdout_segment_ids(n: int = 20) -> set[str]:
    if not _vseg_labels_path().exists():
        return set()
    data = json.loads(_vseg_labels_path().read_text(encoding="utf-8"))
    feedback = [f for f in data.get("feedback", []) if f.get("segment_id")]
    feedback.sort(key=lambda r: str(r.get("at", "")))
    if len(feedback) <= n:
        return {str(r["segment_id"]) for r in feedback[-n:]}
    scored: list[tuple[str, str]] = []
    for row in feedback:
        sid = str(row["segment_id"])
        h = hashlib.sha256(sid.encode()).hexdigest()
        scored.append((h, sid))
    scored.sort(key=lambda x: x[0])
    return {sid for _, sid in scored[:n]}


def eval_holdout_precision(*, holdout_n: int = 20, min_precision: float = 0.45) -> dict:
    from score_owner_windows import score_window_row

    if not _vseg_labels_path().exists():
        return {"pass": False, "reason": "no_labels", "precision": 0.0}

    data = json.loads(_vseg_labels_path().read_text(encoding="utf-8"))
    holdout = _holdout_segment_ids(holdout_n)
    good_pass = bad_pass = good_total = bad_total = 0
    rows: list[dict] = []

    for bucket, label in (("good", "good"), ("bad", "bad")):
        for entry in data.get(bucket, []):
            sid = str(entry.get("segment_id", ""))
            if sid not in holdout:
                continue
            vod_path = Path(str(entry.get("vod", "")))
            vid = vod_path.stem[3:] if vod_path.name.startswith("yt_") else sid.rsplit("_", 1)[0]
            vod = resolve_vod(vid)
            if not vod:
                continue
            start = float(entry.get("start") or 0)
            scored = score_window_row(PROFILE, vid, label, start, vod)
            rows.append(scored)
            if label == "good":
                good_total += 1
                if scored["pass"]:
                    good_pass += 1
            else:
                bad_total += 1
                if scored["pass"]:
                    bad_pass += 1

    would_send = good_pass + bad_pass
    precision = good_pass / would_send if would_send else 0.0
    return {
        "pass": precision >= min_precision and (good_total + bad_total) >= min(holdout_n, 10),
        "precision": round(precision, 4),
        "min_precision": min_precision,
        "holdout_n": len(holdout),
        "evaluated": len(rows),
        "good_pass": good_pass,
        "bad_false_pass": bad_pass,
        "good_total": good_total,
        "bad_total": bad_total,
    }


def dry_run_gate_rejection(
    *,
    n_candidates: int = 10,
    min_rejected: int = 7,
    vod: Path | None = None,
) -> dict:
    from preview_gate import validate_clips_before_preview
    from strict_montage_direct import discover_strict_candidates, file_sha256

    if vod is None:
        for mp4 in sorted(INBOX.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            if _ffprobe_duration(mp4) >= float(os.environ.get("MLBB_VOD_MIN_SEC", "900")):
                vod = mp4
                break
    if vod is None or not vod.exists():
        return {"pass": False, "reason": "no_vod", "tested": 0, "rejected": 0}

    sig = file_sha256(vod)
    pool = discover_strict_candidates(vod, PROFILE, sig, set())
    lead_sec = float(os.environ.get("MLBB_VOD_LEAD_SEC", "12"))
    tested = rejected = passed = 0
    details: list[dict] = []

    for clip in pool:
        if tested >= n_candidates:
            break
        peak = float(clip.get("start", 0))
        start = max(0.0, peak - lead_sec)
        lead_clip = {**clip, "start": start, "peak_start": peak}
        ok, reason, _, _metrics, _vis = validate_clips_before_preview(vod, PROFILE, [lead_clip])
        tested += 1
        if ok:
            passed += 1
        else:
            rejected += 1
        details.append({"start": round(start, 1), "peak": round(peak, 1), "pass": ok, "reason": reason})

    return {
        "pass": tested >= n_candidates and rejected >= min_rejected,
        "vod": str(vod),
        "tested": tested,
        "rejected": rejected,
        "passed": passed,
        "min_rejected": min_rejected,
        "details": details[:12],
    }


def _ffprobe_duration(path: Path) -> float:
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
        timeout=30,
    )
    try:
        return float((proc.stdout or "0").strip())
    except ValueError:
        return 0.0


def eval_transition_gate(*, write_report: bool = True) -> dict:
    bad = eval_bad_block_tests()
    holdout = eval_holdout_precision()
    dry = dry_run_gate_rejection()
    prec7 = precision_7d()
    all_pass = bad["pass"] and holdout["pass"] and dry["pass"]
    report = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "learning_first": enabled(),
        "transition_passed": transition_passed(),
        "all_pass": all_pass,
        "precision_7d": round(prec7, 4),
        "max_daily_sends": max_daily_sends(),
        "bad_block": bad,
        "holdout": holdout,
        "dry_run": dry,
    }
    if write_report:
        _gate_report_path().parent.mkdir(parents=True, exist_ok=True)
        _gate_report_path().write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        state = load_state()
        state["last_gate"] = report
        state["last_precision_7d"] = prec7
        if all_pass and enabled() and not state.get("transition_passed"):
            state["transition_passed"] = True
            state["transition_at"] = report["at"]
        save_state(state)
    return report


def dislike_feedback_report(
    segment_id: str,
    *,
    vod_id: str = "",
    peak_sec: float = 0.0,
    reason: str = "",
) -> str:
    """Owner report after 👎: block zone + thresholds (no sendVideo in LEARNING_FIRST)."""
    pad = _bad_pad_sec()
    vid = vod_id or (segment_id.rsplit("_", 1)[0] if "_" in segment_id else segment_id[:11])
    peak = peak_sec or float(segment_id.rsplit("_", 1)[-1] if "_" in segment_id else 0)
    lo = max(0.0, peak - pad)
    hi = peak + pad
    prec = precision_7d()
    cap = max_daily_sends()
    sent_today = daily_send_count()
    gate = load_state().get("last_gate", {})
    lines = [
        "🛑 LEARNING_FIRST — 👎 записан",
        f"Кусок: {segment_id}",
        f"Блок на VOD {vid}: {lo:.0f}–{hi:.0f}s (±{pad:.0f}s от пика {peak:.0f}s)",
        f"Причина: {reason or '—'}",
        "",
        "Пороги:",
        f"  HIGHLIGHT_OWNER_BAD_PAD_SEC={pad:.0f}",
        f"  HIGHLIGHT_BAD_EXEMPLAR_LAMBDA={os.environ.get('HIGHLIGHT_BAD_EXEMPLAR_LAMBDA', '0.5')}",
        f"  precision_7d={prec:.0%} (цель ≥45%)",
        f"  send сегодня: {sent_today}/{cap}",
    ]
    if enabled() and not transition_passed():
        lines.append("")
        lines.append("⛔ sendVideo ЗАБЛОКИРОВАН до прохождения gate:")
        bb = gate.get("bad_block", {})
        ho = gate.get("holdout", {})
        dr = gate.get("dry_run", {})
        lines.append(f"  bad_block: {'✅' if bb.get('pass') else '❌'}")
        lines.append(f"  holdout precision: {ho.get('precision', 0):.0%} (need ≥45%)")
        lines.append(f"  dry-run rejected: {dr.get('rejected', 0)}/{dr.get('tested', 0)} (need ≥7/10)")
    elif gate:
        lines.append("")
        lines.append(f"Gate: {'✅ отправки разрешены' if transition_passed() else '❌'}")
    return "\n".join(lines)
