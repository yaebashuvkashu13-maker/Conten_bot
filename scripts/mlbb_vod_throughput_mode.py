#!/usr/bin/env python3
"""Durable throughput mode — silence clock beats heartbeat/OCR 'liveness'.

Overnight the feed stays 'alive' (heartbeat/progress moves) while Telegram
sends stay at zero because strict double-tier OCR + quality gates reject
everything. This module is the single invariant:

  if no successful send for MLBB_THROUGHPUT_SILENCE_SEC → force motion path
  until a send succeeds. Title / quality / discover must not re-arm strict.

Persisted via flag file so restarts/watchdogs keep unlock engaged.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

DEFAULT_SILENCE_SEC = 1800.0  # 30 minutes
FLAG_NAME = "vod_throughput_unlock.json"

# Full override set — must be applied atomically and re-applied after title gate.
THROUGHPUT_OVERRIDES: dict[str, str] = {
    "MLBB_VOD_THROUGHPUT_MODE": "1",
    "MLBB_VOD_DISABLE_SOFTEN": "0",
    "MLBB_VOD_QUALITY_MODE": "0",
    "MLBB_FEEDBACK_GATE": "0",
    "MLBB_VOD_TITLE_MIN_TIER": "0",
    "MLBB_TITLE_SAVAGE_MIN_TIER": "0",
    "MLBB_KILL_BANNER_MIN_TIER": "single",
    "MLBB_KILL_BANNER_REQUIRED": "0",
    "MLBB_VOD_MOTION_ANCHOR_OK": "1",
    "MLBB_VOD_BANNER_PRESEND": "0",
    "MLBB_VOD_BANNER_DISCOVER": "0",
    "MLBB_VOD_BANNER_PREFILTER": "0",
    "MLBB_BANNER_POV_MATCH": "0",
    "MLBB_VOD_SKIP_ON_DISCOVER_MISS": "1",
    "MLBB_VOD_BANNER_SKIP_ON_MISS": "1",
    "MLBB_KILL_BANNER_DISCOVER_PEAK_HINTS": "0",
    "MLBB_KILL_BANNER_FORCE_OCR_DEEP": "0",
    "MLBB_KILL_BANNER_OCR_FAST": "1",
    "MLBB_KILL_BANNER_FORCE_SINGLE_OFFSET": "1",
    "MLBB_KILL_BANNER_THROUGHPUT_MAX_PROBES": "6",
    "MLBB_KILL_BANNER_THROUGHPUT_MAX_SEC": "60",
    "MLBB_VOD_MIN_CLIP_SCORE": "0.03",
    "MLBB_BANNER_MIN_HOOK": "0.03",
    "MLBB_VOD_CIRCUIT_ALLOW_RESET": "0",
}


def data_root() -> Path:
    return Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))


def flag_path() -> Path:
    return Path(os.environ.get("MLBB_THROUGHPUT_FLAG_PATH", str(data_root() / FLAG_NAME)))


def silence_threshold_sec() -> float:
    return max(300.0, float(os.environ.get("MLBB_THROUGHPUT_SILENCE_SEC", str(DEFAULT_SILENCE_SEC))))


def last_send_age_sec() -> float | None:
    """Seconds since last successful MLBB feed send, or None if unknown.

    Unknown must NOT force unlock — that broke tests and sticky-softened every
    boot before the first send. Watchdog/streak paths still engage unlock.
    """
    sent_path = data_root() / "vod_segment_feed_sent.json"
    try:
        if sent_path.exists():
            data = json.loads(sent_path.read_text(encoding="utf-8"))
            ts = str(data.get("updated_at") or "")
            if ts:
                return max(0.0, time.time() - time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S")))
    except Exception:
        pass
    log_path = data_root() / "mlbb_vod_segment_feed.log"
    try:
        if log_path.exists():
            ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
            sent_re = re.compile(r"sent=(\d+) vod=")
            last_ts = 0.0
            for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-20000:]:
                tm = ts_re.match(line)
                if not tm:
                    continue
                try:
                    line_ts = time.mktime(time.strptime(tm.group(1), "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    continue
                sm = sent_re.search(line)
                if sm and int(sm.group(1)) > 0:
                    last_ts = max(last_ts, line_ts)
            if last_ts > 0:
                return max(0.0, time.time() - last_ts)
    except Exception:
        pass
    return None


def flag_active() -> bool:
    path = flag_path()
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(data.get("active"))
    except Exception:
        return True  # presence alone means unlock


def write_flag(*, reason: str, send_age: float | None = None) -> None:
    path = flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    age = send_age if send_age is not None else last_send_age_sec()
    payload = {
        "active": True,
        "reason": reason,
        "send_age_sec": float(age if age is not None else -1.0),
        "set_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pid": os.getpid(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def clear_flag() -> None:
    path = flag_path()
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def silence_locked() -> bool:
    """True when persisted silence/flag demand unlock — ignores transient env."""
    if flag_active():
        return True
    age = last_send_age_sec()
    return age is not None and age >= silence_threshold_sec()


def should_engage(*, adaptive_streak: int = 0, zero_send_streak: int = 0) -> bool:
    if silence_locked():
        return True
    if os.environ.get("MLBB_VOD_THROUGHPUT_MODE", "0") == "1":
        return True
    relax_after = int(os.environ.get("MLBB_RELAX_AFTER_ZERO_VODS", "2"))
    if adaptive_streak >= relax_after or zero_send_streak >= relax_after:
        return True
    return False


def is_active() -> bool:
    """True while unlock should stay applied (env, flag, or silence clock)."""
    if os.environ.get("MLBB_VOD_THROUGHPUT_MODE", "0") == "1":
        return True
    return silence_locked()


def apply_throughput_mode(*, reason: str = "silence") -> dict[str, str]:
    """Force motion/throughput env. Returns applied overrides."""
    age = last_send_age_sec()
    write_flag(reason=reason, send_age=age)
    os.environ.update(THROUGHPUT_OVERRIDES)
    return dict(THROUGHPUT_OVERRIDES)


def ensure_throughput_env() -> bool:
    """Re-apply throughput overrides if mode should be on. Returns True if engaged."""
    if not should_engage(
        adaptive_streak=int(os.environ.get("MLBB_VOD_ADAPTIVE_STREAK_HINT", "0") or 0)
    ):
        return False
    apply_throughput_mode(reason="ensure")
    return True


def mark_send_success() -> None:
    """Clear unlock after a real send so quality gates can return."""
    clear_flag()
    os.environ.pop("MLBB_VOD_THROUGHPUT_MODE", None)


def heartbeat_extra() -> dict:
    age = last_send_age_sec()
    return {
        "last_send_age_sec": round(age, 1) if age is not None else None,
        "throughput_mode": 1 if is_active() else 0,
        "throughput_silence_sec": silence_threshold_sec(),
    }
