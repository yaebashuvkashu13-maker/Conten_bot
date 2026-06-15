#!/usr/bin/env python3
"""Learning-first send policy: daily caps, precision gate, retrain state."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

DATA_ROOT = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
STATE_PATH = Path(os.environ.get("MLBB_LEARNING_STATE", str(DATA_ROOT / "mlbb_learning_state.json")))
LABELS_PATH = Path(os.environ.get("MLBB_CALIBRATION_LABELS", str(DATA_ROOT / "calibration_labels.json")))
RETRAIN_STATE_PATH = Path(os.environ.get("MLBB_RETRAIN_STATE", str(DATA_ROOT / "mlbb_retrain_state.json")))


def _read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _today_key() -> str:
    return time.strftime("%Y-%m-%d")


def _load_state() -> dict:
    state = _read_json(STATE_PATH, {"days": {}, "updated_at": ""})
    state.setdefault("days", {})
    return state


def _save_state(state: dict) -> None:
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(STATE_PATH, state)


def enabled() -> bool:
    return os.environ.get("MLBB_LEARNING_FIRST", "1") == "1"


def max_daily_sends() -> int:
    return int(os.environ.get("MLBB_MAX_DAILY_SENDS", "500"))


def min_precision_7d() -> float:
    return float(os.environ.get("MLBB_MIN_PRECISION_7D", "0.45"))


def daily_send_count() -> int:
    state = _load_state()
    day = state["days"].get(_today_key(), {})
    return int(day.get("sent", 0))


def record_send(count: int = 1) -> None:
    if count <= 0:
        return
    state = _load_state()
    key = _today_key()
    day = state["days"].setdefault(key, {"sent": 0})
    day["sent"] = int(day.get("sent", 0)) + count
    # Keep last 14 days only.
    cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    state["days"] = {k: v for k, v in state["days"].items() if k >= cutoff}
    _save_state(state)


def _parse_label_time(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _feedback_rows() -> list[dict]:
    data = _read_json(LABELS_PATH, {"feedback": []})
    rows = data.get("feedback", [])
    return rows if isinstance(rows, list) else []


def precision_7d() -> float:
    """Owner 👍 rate over last 7 days (Shorts + stored feedback)."""
    cutoff = datetime.now() - timedelta(days=7)
    yes = no = 0
    for row in _feedback_rows():
        label = row.get("owner_label")
        if label not in ("yes", "good", "no", "bad"):
            continue
        ts = _parse_label_time(str(row.get("at", "")))
        if ts is None or ts < cutoff:
            continue
        if label in ("yes", "good"):
            yes += 1
        else:
            no += 1
    total = yes + no
    if total == 0:
        return 1.0
    return yes / total


def feedback_week_counts() -> dict[str, int]:
    cutoff = datetime.now() - timedelta(days=7)
    yes = no = 0
    for row in _feedback_rows():
        label = row.get("owner_label")
        if label not in ("yes", "good", "no", "bad"):
            continue
        ts = _parse_label_time(str(row.get("at", "")))
        if ts is None or ts < cutoff:
            continue
        if label in ("yes", "good"):
            yes += 1
        else:
            no += 1
    return {"yes": yes, "no": no, "total": yes + no}


def sends_allowed() -> bool:
    ok, _ = can_send(1)
    return ok


def can_send(count: int = 1) -> tuple[bool, str]:
    if os.environ.get("MLBB_SEND_ENABLED", "1") != "1":
        return False, "send_disabled"
    if count <= 0:
        return True, "ok"
    cap = max_daily_sends()
    sent = daily_send_count()
    if sent + count > cap:
        return False, f"daily_cap={sent}/{cap}"
    if not enabled():
        return True, "ok"
    prec = precision_7d()
    min_prec = min_precision_7d()
    if prec < min_prec and sent >= int(os.environ.get("MLBB_LEARNING_LOW_PREC_CAP", "12")):
        return False, f"precision_7d={prec:.0%}<{min_prec:.0%}"
    return True, "ok"


def dislike_feedback_report(
    segment_id: str,
    *,
    vod_id: str = "",
    peak_sec: float = 0.0,
    reason: str = "",
) -> str:
    """Short owner hint after VOD 👎."""
    parts = [f"Записал 👎 для {segment_id}"]
    if vod_id:
        parts.append(f"VOD: {vod_id}")
    if peak_sec > 0:
        parts.append(f"пик ~{int(peak_sec)}с")
    if reason:
        parts.append(f"причина: {reason}")
    prec = precision_7d()
    parts.append(f"precision_7d: {prec:.0%}")
    return "\n".join(parts)


def eval_transition_gate() -> dict:
    """Holdout check before widening send volume."""
    rows = _feedback_rows()
    yes_rows = [r for r in rows if r.get("owner_label") in ("yes", "good")]
    no_rows = [r for r in rows if r.get("owner_label") in ("no", "bad")]
    holdout_n = min(len(yes_rows), len(no_rows), 20)
    holdout_prec = 0.0
    if holdout_n > 0:
        holdout_prec = len(yes_rows) / max(len(yes_rows) + len(no_rows), 1)

    dry_tested = dry_rejected = 0
    try:
        from mlbb_calibration_store import pending_candidates

        for row in pending_candidates(limit=30, repair=False):
            dry_tested += 1
            score = float(row.get("score") or 0)
            if score < float(os.environ.get("MLBB_CALIBRATION_MIN_SCORE", "0.12")):
                dry_rejected += 1
    except ImportError:
        pass

    min_prec = float(os.environ.get("MLBB_EVAL_MIN_PRECISION", "0.50"))
    min_labels = int(os.environ.get("MLBB_EVAL_MIN_LABELS", "40"))
    all_pass = (
        len(yes_rows) + len(no_rows) >= min_labels
        and holdout_prec >= min_prec
        and precision_7d() >= min_prec
    )
    return {
        "all_pass": all_pass,
        "holdout": {"precision": round(holdout_prec, 4), "n": holdout_n},
        "dry_run": {"tested": dry_tested, "rejected": dry_rejected},
        "labels": {"yes": len(yes_rows), "no": len(no_rows)},
        "precision_7d": round(precision_7d(), 4),
    }


def retrain_state() -> dict:
    return _read_json(
        RETRAIN_STATE_PATH,
        {"labels_since_retrain": 0, "last_retrain_at": 0.0, "pending": False},
    )


def save_retrain_state(state: dict) -> None:
    _write_json(RETRAIN_STATE_PATH, state)


def record_label_for_retrain() -> None:
    state = retrain_state()
    state["labels_since_retrain"] = int(state.get("labels_since_retrain", 0)) + 1
    save_retrain_state(state)


def should_run_retrain(*, force: bool = False) -> tuple[bool, str]:
    if force:
        return True, "forced"
    state = retrain_state()
    min_labels = int(os.environ.get("MLBB_RETRAIN_MIN_LABELS", "10"))
    min_hours = float(os.environ.get("MLBB_RETRAIN_MIN_HOURS", "6"))
    since = int(state.get("labels_since_retrain", 0))
    last = float(state.get("last_retrain_at", 0))
    age_h = (time.time() - last) / 3600.0 if last > 0 else 1e9
    if since >= min_labels:
        return True, f"labels={since}>={min_labels}"
    if last > 0 and age_h >= min_hours and since > 0:
        return True, f"age={age_h:.1f}h>={min_hours}h labels={since}"
    return False, f"wait labels={since}/{min_labels} age={age_h:.1f}h"


def mark_retrain_started() -> None:
    state = retrain_state()
    state["pending"] = True
    state["retrain_started_at"] = time.time()
    save_retrain_state(state)


def mark_retrain_finished(*, ok: bool = True) -> None:
    state = retrain_state()
    state["pending"] = False
    state["last_retrain_at"] = time.time()
    state["labels_since_retrain"] = 0
    state["last_retrain_ok"] = bool(ok)
    save_retrain_state(state)
