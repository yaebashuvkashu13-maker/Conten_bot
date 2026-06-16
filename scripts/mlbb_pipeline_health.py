#!/usr/bin/env python3
"""Pipeline health state — steady pacing + autonomic recovery signals."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

DATA = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))


def _health_path() -> Path:
    return Path(os.environ.get("MLBB_PIPELINE_HEALTH", str(DATA / "mlbb_pipeline_health.json")))


def _read() -> dict:
    path = _health_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict) -> None:
    path = _health_path()
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def steady_mode() -> bool:
    return os.environ.get("MLBB_STEADY_MODE", "1") == "1"


def learning_spam_mode() -> bool:
    return os.environ.get("MLBB_LEARNING_SPAM_MODE", "0") == "1"


def steady_feed_interval_sec() -> float:
    if learning_spam_mode():
        return float(os.environ.get("MLBB_STEADY_FEED_INTERVAL_SEC", "1680"))
    return float(os.environ.get("MLBB_STEADY_FEED_INTERVAL_SEC", "720"))


def max_silence_sec() -> float:
    if learning_spam_mode():
        return float(os.environ.get("MLBB_MAX_SILENCE_SEC", "1800"))
    return float(os.environ.get("MLBB_MAX_SILENCE_SEC", "5400"))


def snapshot() -> dict:
    return dict(_read())


def record_feed_delivery(*, delivered: int, skipped_unsendable: int = 0) -> None:
    data = _read()
    now = time.time()
    if delivered > 0:
        data["last_feed_delivered_at"] = now
        data["last_feed_delivered_count"] = int(delivered)
        data["total_batches"] = int(data.get("total_batches", 0)) + 1
        data["total_delivered"] = int(data.get("total_delivered", 0)) + int(delivered)
        data["consecutive_empty_feeds"] = 0
        data["consecutive_unsendable_feeds"] = 0
    else:
        data["last_feed_empty_at"] = now
        data["consecutive_empty_feeds"] = int(data.get("consecutive_empty_feeds", 0)) + 1
        if skipped_unsendable > 0:
            data["consecutive_unsendable_feeds"] = int(data.get("consecutive_unsendable_feeds", 0)) + 1
            data["last_feed_unsendable_at"] = now
            data["last_feed_unsendable_count"] = int(skipped_unsendable)
        else:
            data["consecutive_unsendable_feeds"] = 0
    data["last_feed_run_at"] = now
    _write(data)


def record_ingest_saved(*, count: int = 1) -> None:
    if count <= 0:
        return
    data = _read()
    data["last_ingest_save_at"] = time.time()
    data["ingest_saves_total"] = int(data.get("ingest_saves_total", 0)) + int(count)
    _write(data)


def record_recovery(*, reason: str, actions: list[str]) -> None:
    data = _read()
    data["last_recovery_at"] = time.time()
    data["last_recovery_reason"] = reason
    data["last_recovery_actions"] = actions
    data["recovery_count"] = int(data.get("recovery_count", 0)) + 1
    _write(data)


def last_feed_delivered_at() -> float:
    return float(_read().get("last_feed_delivered_at") or 0.0)


def silence_sec() -> float:
    last = last_feed_delivered_at()
    if last <= 0:
        return 0.0
    return max(0.0, time.time() - last)


def should_send_feed_steady(*, pending: int, batch_size: int) -> tuple[bool, str]:
    """Steady pacing: send partial batches; never block forever on pending < batch."""
    if not steady_mode():
        return True, "steady_off"
    if pending <= 0:
        return False, "pending=0"
    min_pending = int(os.environ.get("MLBB_STEADY_MIN_SEND_PENDING", "1"))
    if pending < min_pending:
        return False, f"pending={pending}<{min_pending}"
    silence = silence_sec()
    max_s = max_silence_sec()
    # Long silence — send whatever is queued (even 1 clip).
    if silence >= float(os.environ.get("MLBB_STEADY_FORCE_SEND_SILENCE_SEC", str(max_s * 0.25))):
        return True, f"steady_silence={silence:.0f}s"
    last = last_feed_delivered_at()
    interval = steady_feed_interval_sec()
    if last > 0:
        since = time.time() - last
        if since < interval and pending < batch_size:
            return False, f"steady_wait={interval - since:.0f}s pending={pending}<{batch_size}"
    return True, "ok"


def ingest_cooldown_steady() -> float:
    return float(os.environ.get("MLBB_STEADY_INGEST_COOLDOWN_SEC", "300"))


def needs_recovery(*, pending: int) -> tuple[bool, str]:
    data = _read()
    now = time.time()
    silence = silence_sec()
    max_s = max_silence_sec()

    if last_feed_delivered_at() > 0 and silence >= max_s:
        return True, f"no_delivery_{silence:.0f}s"

    unsendable_streak = int(data.get("consecutive_unsendable_feeds") or 0)
    if unsendable_streak >= int(os.environ.get("MLBB_UNSENDABLE_FEED_RECOVERY", "5")):
        return True, f"unsendable_feed_streak={unsendable_streak}"

    if pending == 0:
        last_run = float(data.get("last_feed_run_at") or 0.0)
        if last_run > 0 and (now - last_run) > float(os.environ.get("MLBB_ZERO_PENDING_RECOVERY_SEC", "300")):
            return True, f"pending_zero_{now - last_run:.0f}s"

    last_ingest = float(data.get("last_ingest_save_at") or 0.0)
    if pending < int(os.environ.get("MLBB_STEADY_MIN_PENDING", "4")):
        if last_ingest > 0 and (now - last_ingest) > float(os.environ.get("MLBB_INGEST_STALL_SEC", "1800")):
            return True, f"ingest_stall_pending={pending}"
        if last_ingest <= 0 and (now - float(data.get("last_feed_run_at") or now)) > 600:
            empty_streak = int(data.get("consecutive_empty_feeds") or 0)
            if empty_streak >= int(os.environ.get("MLBB_EMPTY_FEED_RECOVERY", "8")):
                return True, f"empty_feed_streak={empty_streak}"

    # Pending but feed blocked (steady wait) — recover after queue wait.
    if pending > 0 and pending < int(os.environ.get("MLBB_CALIBRATION_BATCH", "4")):
        blocked_since = float(data.get("pending_blocked_since") or 0.0)
        if blocked_since <= 0:
            data["pending_blocked_since"] = now
            _write(data)
        elif (now - blocked_since) > float(os.environ.get("MLBB_PENDING_BLOCKED_RECOVERY_SEC", "900")):
            return True, f"pending_blocked_{pending}_for_{now - blocked_since:.0f}s"
    elif pending >= int(os.environ.get("MLBB_CALIBRATION_BATCH", "4")):
        if data.get("pending_blocked_since"):
            data.pop("pending_blocked_since", None)
            _write(data)

    last_recovery = float(data.get("last_recovery_at") or 0.0)
    if last_recovery > 0 and (now - last_recovery) < float(os.environ.get("MLBB_RECOVERY_COOLDOWN_SEC", "600")):
        # Silence / blocked-queue recovery bypasses cooldown.
        if silence < max_s * 0.5 and not data.get("pending_blocked_since"):
            return False, "recovery_cooldown"

    return False, "ok"
