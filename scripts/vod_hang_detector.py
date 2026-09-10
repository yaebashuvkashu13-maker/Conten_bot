#!/usr/bin/env python3
"""Detect VOD feed hangs (alive but not shipping) and auto-unload/recover."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vod_feed_recover import (  # noqa: E402
    bump_scan_cooldowns,
    clear_discovery_pauses,
    clear_feed_locks,
    clear_stale_owner_batch_lock,
    feed_log_path,
    feed_process_alive,
    park_exhausted_inbox,
    run_recover,
    unpark_ready_vods,
)
from vod_game_registry import VOD_GAMES, inbox_video_ids, load_state, save_state, spec

DEFAULT_HEARTBEAT = Path("/root/data/mlbb/vod_feed_heartbeat.json")
DEFAULT_HEAL_STAMP = Path("/root/data/mlbb/vod_auto_heal.json")
DEFAULT_ALERT_STAMP = Path("/root/data/mlbb/vod_silence_alert.json")
DEFAULT_DETECT_STAMP = Path("/root/data/mlbb/vod_hang_detect_last.json")
DEFAULT_AUTO_RECOVER_LOG = Path("/root/data/mlbb/hang_recover_auto.log")
SEND_LINE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?"
    r"(?:pipeline done sent=([1-9]\d*)|PUBG sent=([1-9]\d*)|sent=([1-9]\d*) vods=1)",
)
_RECOVER_SENT_RE = re.compile(r"отправка\s+\w+:\s*([1-9]\d*)\s*клип")
PIPELINE_DONE_RE = re.compile(
    r"(?:^|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[, ]\d*\s*\w*\s*)pipeline done sent=(\d+)"
)
PIPELINE_DONE_PLAIN_RE = re.compile(r"pipeline done sent=(\d+)")
LOG_TS = "%Y-%m-%d %H:%M:%S"
CHILD_PATTERNS = ("ffmpeg", "yt-dlp", "youtube-dl")


@dataclass
class HangReport:
    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    last_send_age_sec: float | None = None
    heartbeat_age_sec: float | None = None
    log_age_sec: float | None = None
    zero_send_streak: int = 0
    stuck_children: list[dict] = field(default_factory=list)
    stuck_parts: list[str] = field(default_factory=list)
    feed_alive: bool = False

    def add(self, reason: str) -> None:
        self.ok = False
        if reason not in self.reasons:
            self.reasons.append(reason)


def _now() -> float:
    return time.time()


def _parse_log_ts(line: str) -> float | None:
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), LOG_TS))
    except ValueError:
        return None


def heartbeat_path() -> Path:
    return Path(os.environ.get("VOD_FEED_HEARTBEAT_PATH", str(DEFAULT_HEARTBEAT)))


def write_heartbeat(game: str, phase: str, **extra: object) -> None:
    path = heartbeat_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"game": game, "phase": phase, "ts": _now(), **extra}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def read_heartbeat() -> dict:
    path = heartbeat_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def last_send_age_sec() -> float:
    now = _now()
    best = float(os.environ.get("VOD_SILENCE_MAX_AGE_SEC", "604800"))

    for game in VOD_GAMES:
        sent_path = spec(game).feed_sent_path()
        try:
            exists = sent_path.is_file()
        except OSError:
            continue
        if not exists:
            continue
        try:
            data = json.loads(sent_path.read_text(encoding="utf-8"))
            ts = str(data.get("updated_at") or "").strip()
            if ts:
                best = min(best, now - time.mktime(time.strptime(ts, LOG_TS)))
            else:
                best = min(best, now - sent_path.stat().st_mtime)
        except (json.JSONDecodeError, OSError, ValueError):
            try:
                best = min(best, now - sent_path.stat().st_mtime)
            except OSError:
                pass

    log_path = feed_log_path()
    try:
        log_exists = log_path.is_file()
    except OSError:
        log_exists = False
    if log_exists:
        try:
            lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-20000:]
        except OSError:
            lines = []
        for line in reversed(lines):
            m = SEND_LINE_RE.search(line)
            if not m:
                continue
            ts = _parse_log_ts(line)
            if ts is None:
                continue
            best = min(best, now - ts)
            break

    return best


def zero_send_streak(max_lines: int = 40000) -> int:
    """Count consecutive pipeline done sent=0 from log tail.

    Feed prints bare `pipeline done sent=N ...` via print() — no logging
    timestamp — so matching must not require a leading datetime.
    """
    from path_safe import is_file as path_is_file

    log_path = feed_log_path()
    if not path_is_file(log_path):
        return 0
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]
    except OSError:
        return 0
    streak = 0
    for line in reversed(lines):
        m = PIPELINE_DONE_PLAIN_RE.search(line)
        if not m:
            continue
        sent = int(m.group(1))
        if sent > 0:
            break
        streak += 1
    return streak


def _proc_etime_sec(pid: int) -> float | None:
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etime="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        raw = (out.stdout or "").strip()
        if not raw:
            return None
        if re.match(r"^\d+:\d{2}$", raw):
            mm, ss = raw.split(":")
            return int(mm) * 60 + int(ss)
        if re.match(r"^\d+:\d{2}:\d{2}$", raw):
            hh, mm, ss = raw.split(":")
            return int(hh) * 3600 + int(mm) * 60 + int(ss)
        if re.match(r"^\d+-\d{2}:\d{2}:\d{2}$", raw):
            day, rest = raw.split("-", 1)
            hh, mm, ss = rest.split(":")
            return int(day) * 86400 + int(hh) * 3600 + int(mm) * 60 + int(ss)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return None


def find_stuck_children(min_age_sec: int | None = None) -> list[dict]:
    min_age = min_age_sec or max(120, int(os.environ.get("VOD_CHILD_STUCK_SEC", "600")))
    stuck: list[dict] = []
    for pid_name in os.listdir("/proc"):
        if not pid_name.isdigit():
            continue
        try:
            raw = Path(f"/proc/{pid_name}/cmdline").read_bytes()
        except OSError:
            continue
        cmd = " ".join(p.decode(errors="ignore") for p in raw.split(b"\0") if p).lower()
        if not any(pat in cmd for pat in CHILD_PATTERNS):
            continue
        age = _proc_etime_sec(int(pid_name))
        if age is None or age < min_age:
            continue
        stuck.append({"pid": int(pid_name), "age_sec": int(age), "cmd": cmd[:200]})
    stuck.sort(key=lambda row: -row["age_sec"])
    return stuck


def find_stuck_part_files(game: str = "pubg", min_age_sec: int | None = None) -> list[str]:
    min_age = min_age_sec or max(300, int(os.environ.get("VOD_PART_STUCK_SEC", "7200")))
    inbox = spec(game).inbox()
    if not inbox.is_dir():
        return []
    now = _now()
    stuck: list[str] = []
    for part in inbox.glob("*.part"):
        try:
            age = now - part.stat().st_mtime
        except OSError:
            continue
        if age >= min_age:
            stuck.append(str(part))
    return stuck


def kill_pids(pids: list[int]) -> int:
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, 9)
            killed += 1
        except OSError:
            pass
    return killed


def stop_feed_processes(game: str = "all") -> None:
    from vod_force_send import _stop_game_feed

    targets = [game] if game != "all" else list(VOD_GAMES)
    seen: set[str] = set()
    for g in targets:
        if g in seen:
            continue
        seen.add(g)
        _stop_game_feed(g)


def isolate_drought_inbox(game: str = "pubg", *, prefer_ids: list[str] | None = None) -> list[str]:
    """On extreme silence keep only preferred VODs in inbox; park the rest.

    Prevents the hang agent from burning hours on junk/new downloads while a
    known-good Metro VOD still has unused peaks.
    """
    prefer = [p for p in (prefer_ids or []) if p]
    if not prefer:
        prefer = [
            x.strip()
            for x in str(os.environ.get("VOD_DROUGHT_PREFER_IDS", "zRQC8jkxbXQ")).split(",")
            if x.strip()
        ]
    if not prefer:
        return []
    inbox = spec(game).inbox()
    if not inbox.is_dir():
        return []
    parked = inbox / "parked"
    parked.mkdir(parents=True, exist_ok=True)
    kept: list[str] = []
    moved: list[str] = []
    prefer_set = set(prefer)
    for mp4 in list(inbox.glob("yt_*.mp4")):
        vid = mp4.stem[3:][:11] if mp4.stem.startswith("yt_") else mp4.stem[:11]
        if vid in prefer_set:
            kept.append(mp4.name)
            continue
        dest = parked / mp4.name
        try:
            if dest.exists():
                mp4.unlink(missing_ok=True)
            else:
                mp4.rename(dest)
            moved.append(mp4.name)
        except OSError:
            continue
    # Pin preferred id when present.
    try:
        state = load_state(game)
        for pref in prefer:
            name = f"yt_{pref}.mp4"
            if (inbox / name).is_file():
                state["pubg_singles_active_vod"] = pref
                for row in state.get("vods") or []:
                    vid = str(row.get("id") or "")
                    if vid == pref:
                        row["exhausted"] = False
                        row.pop("reject_reason", None)
                    elif vid:
                        row["exhausted"] = True
                save_state(game, state)
                break
    except Exception:
        pass
    if moved:
        return moved
    return kept


def unload_stuck_inbox_vod(game: str, *, min_rejects: int = 3) -> str | None:
    """Park inbox VOD that keeps failing (metro_reject loop) so feed can move on."""
    inbox = spec(game).inbox()
    mp4s = sorted(inbox.glob("yt_*.mp4"))
    if len(mp4s) != 1 and len(mp4s) != 0:
        # Multiple inbox files — only unload if one dominates log rejects.
        pass
    if not mp4s:
        return None

    log_path = feed_log_path()
    if not log_path.is_file():
        return None
    try:
        tail = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-4000:]
    except OSError:
        return None

    reject_counts: dict[str, int] = {}
    for line in tail:
        low = line.lower()
        if (
            "metro_reject=1" not in line
            and "sent=0" not in line
            and "presend reject" not in low
            and "hard_no_author_kill" not in low
            and "hard_loot_walk" not in low
            and "hard_menu_overlay" not in low
        ):
            continue
        for mp4 in mp4s:
            vid = mp4.stem[3:][:11] if mp4.stem.startswith("yt_") else mp4.stem[:11]
            if vid and vid in line:
                reject_counts[vid] = reject_counts.get(vid, 0) + 1
                break

    parked_dir = inbox.parent / "parked"
    parked_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(game)
    registry = {str(r.get("id") or ""): r for r in state.get("vods") or []}

    for mp4 in mp4s:
        vid = mp4.stem[3:][:11] if mp4.stem.startswith("yt_") else mp4.stem[:11]
        if reject_counts.get(vid, 0) < min_rejects:
            continue
        dest = parked_dir / mp4.name
        try:
            if dest.exists():
                mp4.unlink(missing_ok=True)
            else:
                mp4.rename(dest)
        except OSError:
            continue
        row = registry.get(vid)
        if row is not None:
            row["exhausted"] = True
            row["reject_reason"] = row.get("reject_reason") or "hang_detector_unload"
            row["last_scan_at"] = _now()
        save_state(game, state)
        return mp4.name
    return None


def remove_stuck_parts(paths: list[str]) -> int:
    removed = 0
    for raw in paths:
        try:
            Path(raw).unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
    return removed


def detect_hang() -> HangReport:
    report = HangReport(feed_alive=feed_process_alive())
    now = _now()

    report.last_send_age_sec = last_send_age_sec()
    hb = read_heartbeat()
    hb_ts = float(hb.get("ts") or 0)
    if hb_ts > 0:
        report.heartbeat_age_sec = now - hb_ts

    log_path = feed_log_path()
    if log_path.is_file():
        try:
            report.log_age_sec = now - log_path.stat().st_mtime
        except OSError:
            pass

    report.zero_send_streak = zero_send_streak()
    report.stuck_children = find_stuck_children()
    report.stuck_parts = find_stuck_part_files("pubg")

    silence_warn = max(600, int(os.environ.get("VOD_SILENCE_WARN_SEC", "3600")))
    # Absolute drought: even a "busy" feed (fresh heartbeat / discovery spam)
    # must heal after this — otherwise live/zero-duration YouTube loops look healthy
    # for many hours while nothing ships.
    absolute_silence = max(
        silence_warn,
        int(os.environ.get("VOD_ABSOLUTE_SILENCE_SEC", "5400")),
    )
    progress_stuck = max(300, int(os.environ.get("VOD_PROGRESS_STUCK_SEC", "900")))
    zero_streak_heal = max(3, int(os.environ.get("VOD_ZERO_SEND_STREAK_HEAL", "6")))
    # If feed is actively working (fresh heartbeat), short silence alone is NOT a hang.
    working = (
        report.feed_alive
        and report.heartbeat_age_sec is not None
        and report.heartbeat_age_sec < progress_stuck
    )

    if report.last_send_age_sec is not None and report.last_send_age_sec >= absolute_silence:
        report.add(f"absolute_silence_{int(report.last_send_age_sec)}s")
    elif report.last_send_age_sec is not None and report.last_send_age_sec >= silence_warn:
        if working and report.zero_send_streak < zero_streak_heal and not report.stuck_children:
            # Actively scanning/downloading — do not false-alarm heal.
            pass
        else:
            report.add(f"silence_{int(report.last_send_age_sec)}s")

    if report.zero_send_streak >= zero_streak_heal:
        # Streak alone must not fire right after a real Telegram send — empty
        # discovery loops still print sent=0 and would re-heal within minutes.
        fresh_send = (
            report.last_send_age_sec is not None
            and report.last_send_age_sec < silence_warn
        )
        if not fresh_send:
            report.add(f"zero_send_streak_{report.zero_send_streak}")

    if report.feed_alive:
        hb_age = report.heartbeat_age_sec
        if hb_age is not None and hb_age >= progress_stuck:
            report.add(f"heartbeat_stuck_{int(hb_age)}s")
        elif hb_age is None and report.log_age_sec is not None and report.log_age_sec >= progress_stuck:
            report.add(f"log_stuck_{int(report.log_age_sec)}s")

    if report.stuck_children:
        report.add(f"stuck_child_{report.stuck_children[0]['age_sec']}s")

    if report.stuck_parts:
        report.add(f"stuck_part_{len(report.stuck_parts)}")

    mined_drought = max(900, int(os.environ.get("VOD_MINED_INBOX_DROUGHT_SEC", "1800")))
    if report.last_send_age_sec is not None and report.last_send_age_sec >= mined_drought:
        try:
            mined = inbox_mined_out("pubg")
        except Exception:
            mined = False
        if mined:
            report.add(f"mined_inbox_drought_{int(report.last_send_age_sec)}s")

    # Discovery-only loop: feed alive, heartbeat fresh, but no scannable VOD and
    # silence past warn — treat as hang so recover can unpark / rediscover.
    discovery_drought = max(silence_warn, int(os.environ.get("VOD_DISCOVERY_DROUGHT_SEC", "5400")))
    if (
        report.last_send_age_sec is not None
        and report.last_send_age_sec >= discovery_drought
        and report.feed_alive
        and not any(r.startswith("absolute_silence_") for r in report.reasons)
    ):
        try:
            inbox = spec("pubg").inbox()
            mp4s = [p for p in inbox.glob("yt_*.mp4") if p.is_file()] if inbox.is_dir() else []
            state = load_state("pubg")
            usable = False
            for mp4 in mp4s:
                vid = mp4.stem[3:][:11] if mp4.stem.startswith("yt_") else mp4.stem[:11]
                row = next((r for r in (state.get("vods") or []) if r.get("id") == vid), {}) or {}
                if row.get("exhausted"):
                    continue
                left = _entry_remaining_peaks("pubg", vid, row)
                if left is None or left > 0:
                    usable = True
                    break
            if not usable:
                report.add(f"discovery_drought_{int(report.last_send_age_sec)}s")
        except Exception:
            pass

    return report


def _entry_remaining_peaks(game: str, vid: str, row: dict) -> int | None:
    peaks = row.get("last_pool_peaks") or []
    if not peaks or not vid:
        return None
    try:
        from shooter_vod_segment_store import load_feed_sent, load_index
        from vod_peak_gap import pool_peak_seconds, used_peak_times_shooter

        sent_set = load_feed_sent(game)
        used = used_peak_times_shooter(vid, sent_set, load_index(game).get("segments", []))
        secs = pool_peak_seconds(peaks)
    except Exception:
        return None
    if not secs:
        return None
    left = 0
    for p in secs:
        if not any(abs(p - u) <= 8.0 for u in used):
            left += 1
    return left


def inbox_mined_out(game: str = "pubg") -> bool:
    """True when every inbox VOD has a known peak pool and zero unsent peaks."""
    inbox = spec(game).inbox()
    if not inbox.is_dir():
        return False
    mp4s = [p for p in inbox.glob("yt_*.mp4") if p.is_file()]
    if not mp4s:
        return False
    state = load_state(game)
    registry = {str(r.get("id") or ""): r for r in state.get("vods") or []}
    mined = 0
    for mp4 in mp4s:
        vid = mp4.stem[3:][:11] if mp4.stem.startswith("yt_") else mp4.stem[:11]
        row = registry.get(vid) or {}
        left = _entry_remaining_peaks(game, vid, row)
        if left is None or left > 0:
            return False
        mined += 1
    return mined > 0


def mark_mined_inbox_exhausted(game: str = "pubg") -> list[str]:
    """Stamp mined-out inbox VODs exhausted so recover can park + unpark."""
    inbox = spec(game).inbox()
    if not inbox.is_dir():
        return []
    state = load_state(game)
    registry = {str(r.get("id") or ""): r for r in state.get("vods") or []}
    marked: list[str] = []
    changed = False
    for mp4 in list(inbox.glob("yt_*.mp4")):
        vid = mp4.stem[3:][:11] if mp4.stem.startswith("yt_") else mp4.stem[:11]
        row = registry.get(vid)
        if row is None:
            continue
        left = _entry_remaining_peaks(game, vid, row)
        if left != 0:
            continue
        row["exhausted"] = True
        row["reject_reason"] = "pubg_mined_out"
        marked.append(mp4.name)
        changed = True
    if changed:
        save_state(game, state)
    return marked


def _read_heal_stamp() -> dict:
    stamp = DEFAULT_HEAL_STAMP
    if not stamp.is_file():
        return {}
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _heal_escalation() -> int:
    try:
        return max(0, min(2, int(_read_heal_stamp().get("escalation") or 0)))
    except (TypeError, ValueError):
        return 0


def _heal_cooldown_ok(min_sec: int | None = None) -> bool:
    # Default 45m between *successful* heals; failed drought recovers retry faster.
    min_sec = min_sec or max(900, int(os.environ.get("VOD_HEAL_COOLDOWN_SEC", "2700")))
    data = _read_heal_stamp()
    if not data:
        return True
    try:
        last = float(data.get("last_heal_ts") or 0)
    except (TypeError, ValueError):
        return True
    age = _now() - last
    if age >= min_sec:
        return True
    # Recover that did not advance last-send must not block the ladder for 45m —
    # otherwise drought + mined inbox looks like "auto-heal never starts".
    if os.environ.get("VOD_HEAL_RETRY_ON_SILENCE", "1") != "1":
        return False
    try:
        send_age = last_send_age_sec()
    except Exception:
        send_age = None
    # Soften window (1h): retry every 10m. Absolute drought (default 1.5h): every 5–10m.
    silence_warn = max(600, int(os.environ.get("VOD_SILENCE_WARN_SEC", "3600")))
    absolute = max(silence_warn, int(os.environ.get("VOD_ABSOLUTE_SILENCE_SEC", "5400")))
    prev_sent = int(data.get("sent") or 0)
    if send_age is not None and send_age >= absolute:
        retry_sec = max(300, int(os.environ.get("VOD_HEAL_RETRY_SEC", "600")))
    elif send_age is not None and send_age >= silence_warn:
        retry_sec = max(600, int(os.environ.get("VOD_HEAL_RETRY_SEC", "600")))
    elif prev_sent <= 0:
        # Last auto-recover shipped nothing — do not sit on the long cooldown.
        retry_sec = max(600, int(os.environ.get("VOD_HEAL_RETRY_SEC", "900")))
    else:
        return False
    if age < retry_sec:
        return False
    if send_age is None:
        return True
    # last_send still older than this heal → previous recover shipped nothing
    return send_age > age + 30


def _mark_heal(action: str, **extra: object) -> None:
    DEFAULT_HEAL_STAMP.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_heal_ts": _now(), "action": action, **extra}
    DEFAULT_HEAL_STAMP.write_text(json.dumps(payload), encoding="utf-8")


def apply_agent_recover_env(
    env: dict[str, str] | None = None,
    *,
    escalation: int | None = None,
) -> dict[str, str]:
    """Env knobs matching the manual agent playbook (soften / skip discovery / escalate)."""
    target: dict[str, str] = env if env is not None else os.environ  # type: ignore[assignment]
    try:
        silence = float(last_send_age_sec() or 0)
    except Exception:
        silence = 0.0
    esc = _heal_escalation() if escalation is None else max(0, min(2, int(escalation)))
    silence_warn = max(600, int(os.environ.get("VOD_SILENCE_WARN_SEC", "3600")))
    drought_sec = max(600, int(os.environ.get("VOD_FORCE_DROUGHT_SEC", "3600")))
    need_soft = (
        silence >= float(drought_sec)
        or silence >= float(silence_warn)
        or esc > 0
        or os.environ.get("VOD_FORCE_SOFTEN", "0") == "1"
    )
    if not need_soft:
        return target
    target["VOD_FORCE_SOFTEN"] = "1"
    target["VOD_PRESEND_CACHE"] = "0"
    # Enable singles gun bypass only while drought soften is active.
    # Hard-assign under soften — setdefault is a no-op when deploy pinned 0.
    target["PUBG_SINGLES_GUN_PAYOFF_BYPASS"] = "1"
    target["PUBG_SINGLES_GUN_QUALITY_BYPASS"] = "1"
    # Never auto-skip discovery or bypass presend — that shipped menu/loot as "keepalive".
    # Soften thresholds only; quality gates stay on.
    # Feed reads SHOOTER_VOD_SKIP_DISCOVERY; also pin the VOD_FORCE_* alias.
    target["VOD_FORCE_SKIP_DISCOVERY"] = "0"
    target["SHOOTER_VOD_SKIP_DISCOVERY"] = "0"
    target["VOD_FORCE_PRESEND_BYPASS"] = "0"
    target["PUBG_PRESEND_SHOOTING_GATE"] = os.environ.get(
        "VOD_FORCE_PRESEND_GATE",
        os.environ.get("PUBG_PRESEND_SHOOTING_GATE", "1"),
    )
    target.setdefault(
        "VOD_FORCE_SEND_MAX_VODS",
        os.environ.get("VOD_FORCE_SEND_MAX_VODS", "4"),
    )
    target.setdefault(
        "VOD_RECOVER_FORCE_SEND_TIMEOUT_SEC",
        os.environ.get("VOD_RECOVER_FORCE_SEND_TIMEOUT_SEC", "1800"),
    )
    target.setdefault(
        "VOD_RECOVER_UNPARK",
        os.environ.get("VOD_RECOVER_UNPARK", "4"),
    )
    target["VOD_FORCE_ESCALATION"] = str(esc)
    # Soften ladder (hard-assign). Must match apply_drought_pubg_env.
    # Do NOT read VOD_FORCE_QUALITY_MIN / PAYOFF_MIN from pinned env — stale
    # strict pins (0.40/0.30) made drought recover stricter than steady state.
    # Keep a real quality floor under drought (0.05 shipped menu junk).
    q_min, p_min, gun = "0.28", "0.08", "0.030"
    if esc >= 1:
        q_min, p_min, gun = "0.24", "0.05", "0.020"
    if esc >= 2:
        q_min, p_min, gun = "0.20", "0.03", "0.010"
    target["VOD_FORCE_QUALITY_MIN"] = q_min
    target["VOD_FORCE_PAYOFF_MIN"] = p_min
    target["VOD_FORCE_GUN_DENSITY"] = gun
    target["VOD_FORCE_BURST_RATIO"] = "3.5"
    target["PUBG_QUALITY_SCORE_MIN_SINGLES"] = q_min
    target["PUBG_PAYOFF_SCORE_MIN_SINGLES"] = p_min
    target["PUBG_FAST_PAYOFF_MIN"] = p_min
    target["PUBG_FAST_RANK_MIN_PAYOFF"] = p_min
    target["PUBG_SINGLE_MIN_GUN_DENSITY"] = gun
    target["PUBG_PRESEND_MIN_GUN_DENSITY"] = gun
    target["PUBG_CLIP_MIN_GUN_DENSITY"] = gun
    target["PUBG_POOL_MIN_GUN_DENSITY"] = gun
    target["SHOOTER_VOD_DENSE_GUN_MIN"] = gun
    target["SMART_PUBG_MIN_GUNFIRE_DENSITY"] = gun
    # Keep loot reject ON at every escalation — garbage menu/loot > silence.
    target["VOD_FORCE_REJECT_LOOT"] = "1"
    target["PUBG_REJECT_LOOT_WALK"] = "1"
    # Hook gate stays ON; only soften HUD false-positive menu score under drought.
    target["CLIP_HOOK_GATE"] = "1"
    hook_menu, hook_rms, hook_ydelta = "0.62", "0.08", "1.5"
    if esc >= 1:
        hook_menu, hook_rms, hook_ydelta = "0.70", "0.05", "1.0"
    if esc >= 2:
        hook_menu, hook_rms, hook_ydelta = "0.78", "0.03", "0.5"
    target["CLIP_HOOK_MAX_MENU"] = hook_menu
    target["CLIP_HOOK_MIN_AUDIO_RMS"] = hook_rms
    target["CLIP_HOOK_MIN_YAVG_DELTA"] = hook_ydelta
    dislike_menu = "0.26"
    if esc >= 1:
        dislike_menu = "0.28"
    if esc >= 2:
        dislike_menu = "0.30"
    target["DISLIKE_MENU_OVERLAY_MAX"] = dislike_menu
    target["PUBG_HARD_REJECT_MENU_OVERLAY"] = "1"
    dislike_gun = "0.040"
    dislike_burst = "4.0"
    if esc >= 1:
        dislike_gun, dislike_burst = "0.025", "3.5"
    if esc >= 2:
        dislike_gun, dislike_burst = "0.015", "3.0"
    target["DISLIKE_GUN_DENSITY_MIN"] = dislike_gun
    target["DISLIKE_BURST_RATIO_MIN"] = dislike_burst
    target["DISLIKE_REASON_GATES"] = "1"
    target["PUBG_COMBAT_TIMELINE"] = "1"
    target["PUBG_EARLY_ACTION_SHIFT"] = "1"
    # Full VOD peak scan — never collapse discovery to kill_pass=8.
    target["PUBG_FULL_PEAK_SCAN"] = "1"
    target["VOD_CASCADE_KILL_MAX"] = "0"
    target["VOD_CASCADE_FAST_RANKER_MAX"] = "0"
    target["CHEAP_CASCADE_TOP_K"] = "0"
    target["CHEAP_CASCADE_HEAVY_TOP"] = "0"
    # Contiguous dense grid — no 40s probe skips / hard max truncation.
    target["SHOOTER_VOD_DENSE_PROBE_STEP_SEC"] = os.environ.get(
        "SHOOTER_VOD_DENSE_PROBE_STEP_SEC", "1.5"
    )
    target["SHOOTER_VOD_DENSE_PROBE_MAX"] = "0"
    target["SHOOTER_VOD_DENSE_PROBE_HARD_MAX"] = "0"
    target["SHOOTER_VOD_FAST_SKIP_INTRO"] = os.environ.get("SHOOTER_VOD_FAST_SKIP_INTRO", "0")
    target["SHOOTER_VOD_AUDIO_CANDIDATE_GAP_SEC"] = os.environ.get(
        "SHOOTER_VOD_AUDIO_CANDIDATE_GAP_SEC", "1"
    )
    target["SHOOTER_VOD_AUDIO_CANDIDATE_MAX"] = "0"
    target["SHOOTER_VOD_DENSE_POOL_BUST"] = os.environ.get("SHOOTER_VOD_DENSE_POOL_BUST", "0")
    # Same as force-send drought: do not fan out 50+ 👍 neighborhood probes.
    target["SHOOTER_VOD_OWNER_NEIGHBORHOOD_PROBE"] = os.environ.get(
        "SHOOTER_VOD_OWNER_NEIGHBORHOOD_PROBE", "0"
    )
    target["SHOOTER_VOD_OWNER_PROBE_MAX"] = os.environ.get(
        "SHOOTER_VOD_OWNER_PROBE_MAX", "8"
    )
    target["PUBG_FAST_RANK_MAX"] = os.environ.get("VOD_FORCE_FAST_RANK_MAX", "24")
    target["PUBG_KILLFEED_RANK_MAX"] = os.environ.get("VOD_FORCE_KILLFEED_RANK_MAX", "16")
    target["PUBG_RANKER_MAX_PROBES"] = os.environ.get("VOD_FORCE_RANKER_MAX_PROBES", "16")
    # 0 = inspect every ranked peak this run (not a silent top-6/8 budget).
    target["PUBG_SINGLES_PEAK_TRIES_PER_RUN"] = os.environ.get(
        "VOD_FORCE_SINGLES_PEAK_TRIES", "0"
    )
    target["PUBG_SINGLES_ZERO_SEND_EXHAUST"] = os.environ.get(
        "VOD_FORCE_SEND_ZERO_EXHAUST", "0"
    )
    # 0 = ship every gate-pass in one cycle (quality flood OK; junk still gated).
    target["PUBG_SINGLES_MAX_SENDS_PER_CYCLE"] = os.environ.get(
        "VOD_FORCE_MAX_SENDS_PER_CYCLE", "0"
    )
    if esc >= 2:
        target["PUBG_FAST_RANK_DROP_LOOT_WALK"] = os.environ.get(
            "PUBG_FAST_RANK_DROP_LOOT_WALK", "0"
        )
        # Keep score-mode ON; never disable shooting gate via recover escalation.
        target["PUBG_PRESEND_SCORE_MODE"] = os.environ.get("VOD_FORCE_PRESEND_SCORE_MODE", "1")
        # Cap owner-relax at 1 while shooting gate stays on (was 2 → menu leak).
        target["PUBG_RELAX_OWNER_HEURISTICS"] = os.environ.get(
            "VOD_FORCE_RELAX_OWNER", "1"
        )
        target["PUBG_PRESEND_SHOOTING_GATE"] = os.environ.get(
            "PUBG_PRESEND_SHOOTING_GATE", "1"
        )
        target["VOD_FORCE_PRESEND_BYPASS"] = "0"
        skip_discovery = os.environ.get("SHOOTER_VOD_SKIP_DISCOVERY") or os.environ.get(
            "VOD_FORCE_SKIP_DISCOVERY"
        )
        if skip_discovery is None:
            target["VOD_FORCE_SKIP_DISCOVERY"] = "0"
            target["SHOOTER_VOD_SKIP_DISCOVERY"] = "0"
        else:
            target["VOD_FORCE_SKIP_DISCOVERY"] = skip_discovery
            target["SHOOTER_VOD_SKIP_DISCOVERY"] = skip_discovery
        target["VOD_PUBG_QUALITY_STRICT"] = "0"
    return target


def _parse_recover_sent(msg: str) -> int:
    match = _RECOVER_SENT_RE.search(msg or "")
    if match:
        return int(match.group(1))
    if "клип(ов) ✅" in (msg or ""):
        return 1
    return 0


def _recover_process_alive() -> bool:
    """True if a hang-detector --recover/--agent child is running (lock may lag)."""
    me = os.getpid()
    for pid_name in os.listdir("/proc"):
        if not pid_name.isdigit():
            continue
        pid = int(pid_name)
        if pid == me:
            continue
        try:
            raw = Path(f"/proc/{pid_name}/cmdline").read_bytes()
        except OSError:
            continue
        parts = [p.decode(errors="ignore") for p in raw.split(b"\0") if p]
        joined = " ".join(parts)
        if "vod_hang_detector.py" not in joined:
            continue
        if "--recover" in joined or "--agent" in joined:
            return True
    return False


def _send_tg(text: str) -> bool:
    try:
        from vod_telegram_env import send_message

        return send_message(text)
    except Exception:
        return False


def _recover_lock_path() -> Path:
    return Path(os.environ.get("VOD_HANG_RECOVER_LOCK", "/tmp/vod_hang_recover.lock"))


def _recover_already_running() -> bool:
    """True if a previous --recover / force_send is still alive."""
    if _recover_process_alive():
        return True
    lock = _recover_lock_path()
    if not lock.is_file():
        return False
    try:
        pid = int(lock.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return False
    if pid <= 1:
        return False
    # Parent may stamp our pid into the lock before we run — not another recover.
    if pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        lock.unlink(missing_ok=True)
        return False


def _spawn_background_recover(game: str) -> bool:
    """Start --recover with agent env; log to hang_recover_auto.log (not /dev/null)."""
    if _recover_already_running():
        return False
    script = Path(__file__).resolve()
    log_path = Path(os.environ.get("VOD_AUTO_RECOVER_LOG", str(DEFAULT_AUTO_RECOVER_LOG)))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    esc = _heal_escalation()
    env = apply_agent_recover_env(dict(os.environ), escalation=esc)
    env["VOD_RECOVER_CHILD"] = "1"
    header = (
        f"\n===== auto recover {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"game={game} esc={esc} silence={int(last_send_age_sec() or 0)}s =====\n"
    )
    try:
        log_fh = log_path.open("a", encoding="utf-8")
    except OSError:
        log_fh = subprocess.DEVNULL  # type: ignore[assignment]
    if log_fh is not subprocess.DEVNULL:
        try:
            log_fh.write(header)
            log_fh.flush()
        except OSError:
            pass
    proc = subprocess.Popen(
        [sys.executable, "-u", str(script), "--recover", "--game", game],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    try:
        _recover_lock_path().write_text(str(proc.pid), encoding="utf-8")
    except OSError:
        pass
    return True


def _acquire_recover_lock() -> bool:
    if _recover_already_running():
        return False
    path = _recover_lock_path()
    try:
        if path.is_file() and int(path.read_text(encoding="utf-8").strip() or "0") == os.getpid():
            return True
    except (OSError, ValueError):
        pass
    path.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _release_recover_lock() -> None:
    path = _recover_lock_path()
    try:
        if path.is_file() and path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _start_systemd_feed() -> None:
    for unit in ("content-bot-vod-feed.service", "mlbb-vod-feed.service"):
        for args in (
            ["enable", unit],
            ["reset-failed", unit],
            ["start", unit],
        ):
            subprocess.run(
                ["systemctl", *args],
                check=False,
                timeout=20,
                capture_output=True,
            )


def _ensure_telegram_bot() -> bool:
    """Recover stop/restart can leave telegram dead — always bring it back."""
    for pid_name in os.listdir("/proc"):
        if not pid_name.isdigit():
            continue
        try:
            raw = Path(f"/proc/{pid_name}/cmdline").read_bytes()
        except OSError:
            continue
        joined = " ".join(p.decode(errors="ignore") for p in raw.split(b"\0") if p)
        if "telegram_upload_bot.py" in joined:
            return True
    for unit in ("telegram-upload-bot.service", "content-bot-telegram.service"):
        try:
            proc = subprocess.run(
                ["systemctl", "start", unit],
                check=False,
                timeout=20,
                capture_output=True,
            )
            if proc.returncode == 0:
                time.sleep(0.5)
                # Fall through to verify via /proc below after start attempt.
        except Exception:
            continue
    bot = Path("/usr/local/bin/telegram_upload_bot.py")
    if not bot.is_file():
        bot = Path(__file__).resolve().parent / "telegram_upload_bot.py"
    if not bot.is_file():
        return False
    # Re-check after systemctl — may already be up.
    for pid_name in os.listdir("/proc"):
        if not pid_name.isdigit():
            continue
        try:
            raw = Path(f"/proc/{pid_name}/cmdline").read_bytes()
        except OSError:
            continue
        joined = " ".join(p.decode(errors="ignore") for p in raw.split(b"\0") if p)
        if "telegram_upload_bot.py" in joined:
            return True
    # Dual-owner risk: never nohup the bot unless explicitly armed.
    if os.environ.get("VOD_FEED_ALLOW_NOHUP", "0") != "1":
        return False
    log = Path("/root/data/mlbb/telegram_upload_bot.log")
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            subprocess.Popen(
                [sys.executable, "-u", str(bot)],
                stdout=fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return True
    except OSError:
        return False


def auto_unload_and_recover(
    report: HangReport,
    *,
    game: str = "pubg",
    force: bool = False,
    background: bool = False,
) -> dict:
    """Execute recovery ladder based on hang class.

    Critical: always respect heal cooldown. Spamming recover every 5 minutes
    kills in-progress downloads and guarantees silence forever.
    """
    if report.ok and not force:
        return {"action": "none", "ok": True}

    # NEVER bypass cooldown for silence — that caused the heal storm.
    cooldown_sec = max(900, int(os.environ.get("VOD_HEAL_COOLDOWN_SEC", "2700")))
    if not force and not _heal_cooldown_ok(cooldown_sec):
        return {"action": "cooldown", "reasons": report.reasons, "cooldown_sec": cooldown_sec}

    if not force and _recover_already_running():
        return {"action": "recover_in_progress", "reasons": report.reasons}

    actions: list[str] = []

    if report.stuck_parts:
        n = remove_stuck_parts(report.stuck_parts)
        if n:
            actions.append(f"removed_parts={n}")

    if report.stuck_children:
        killed = kill_pids([row["pid"] for row in report.stuck_children])
        if killed:
            actions.append(f"killed_children={killed}")

    unloaded = unload_stuck_inbox_vod(game)
    if unloaded:
        actions.append(f"unloaded_inbox={unloaded}")

    mined_marked = mark_mined_inbox_exhausted(game)
    if mined_marked:
        actions.append(f"mined_exhausted={','.join(mined_marked)}")

    for g in ([game] if game != "all" else list(VOD_GAMES)):
        clear_discovery_pauses(g)
        bump_scan_cooldowns(g)
        park_exhausted_inbox(g)
        unparked = unpark_ready_vods(g, limit=max(2, int(os.environ.get("VOD_RECOVER_UNPARK", "4"))))
        if unparked:
            actions.append(f"unpark_{g}={unparked}")

    # Full recover after 1h silence by default (was 90m) — same bar as manual agent work.
    silence_heal = max(1800, int(os.environ.get("VOD_SILENCE_HEAL_SEC", "3600")))
    zero_streak_heal = max(3, int(os.environ.get("VOD_ZERO_SEND_STREAK_HEAL", "6")))
    silence_warn = max(600, int(os.environ.get("VOD_SILENCE_WARN_SEC", "3600")))
    streak_counts = report.zero_send_streak >= zero_streak_heal and (
        report.last_send_age_sec is None or report.last_send_age_sec >= silence_warn
    )
    need_full_recover = force or (
        report.last_send_age_sec is not None and report.last_send_age_sec >= silence_heal
    ) or streak_counts

    if need_full_recover:
        esc = _heal_escalation()
        # Mark heal FIRST so concurrent cron ticks see cooldown immediately.
        _mark_heal("full_recover_pending", sent=0, escalation=esc)
        clear_stale_owner_batch_lock()
        clear_feed_locks()
        if background and os.environ.get("VOD_HEAL_BACKGROUND", "1") == "1":
            if not _spawn_background_recover(game):
                return {
                    "action": "recover_in_progress",
                    "actions": actions,
                    "reasons": report.reasons,
                }
            actions.append(f"full_recover_bg esc={esc}")
            _mark_heal("full_recover_bg", sent=0, escalation=esc)
            return {
                "action": "full_recover_bg",
                "actions": actions,
                "reasons": report.reasons,
                "escalation": esc,
            }
        if not _acquire_recover_lock():
            return {"action": "recover_in_progress", "actions": actions, "reasons": report.reasons}
        try:
            # Same knobs as the human/agent playbook: soften, keep discovery on, escalate.
            apply_agent_recover_env(os.environ, escalation=esc)  # type: ignore[arg-type]
            stop_feed_processes(game)
            msg = run_recover(game, force_send=True)
            sent = _parse_recover_sent(msg)
            next_esc = 0 if sent > 0 else min(2, esc + 1)
            _start_systemd_feed()
            restarted = True
            if _ensure_telegram_bot():
                actions.append("telegram_ok")
            actions.append("full_recover")
            if restarted:
                actions.append("supervisor_restarted")
            if sent > 0:
                actions.append(f"sent={sent}")
            else:
                actions.append(f"sent=0 next_esc={next_esc}")
            _mark_heal("full_recover", sent=sent, escalation=next_esc)
            return {
                "action": "full_recover",
                "actions": actions,
                "reasons": report.reasons,
                "sent": sent,
                "escalation": next_esc,
                "recover_tail": msg.splitlines()[-8:],
            }
        finally:
            _release_recover_lock()
            _ensure_telegram_bot()

    # Light heal: restart feed only — never spam this either.
    _mark_heal("light_restart", sent=0, escalation=_heal_escalation())
    clear_feed_locks()
    stop_feed_processes(game)
    _start_systemd_feed()
    restarted, note = True, "systemd_only"
    _ensure_telegram_bot()
    actions.append(f"light_restart:{note}")
    return {"action": "light_restart", "actions": actions, "reasons": report.reasons}


def _clear_stale_recover_lock() -> bool:
    """Drop dead recover locks so auto-agent is never stuck behind a ghost pid."""
    lock = _recover_lock_path()
    if not lock.is_file():
        return False
    try:
        pid = int(lock.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        lock.unlink(missing_ok=True)
        return True
    if pid <= 1 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
        return False
    except OSError:
        lock.unlink(missing_ok=True)
        return True


def _owner_ops_markup() -> dict | None:
    try:
        from telegram_owner_controls import owner_controls_keyboard

        return owner_controls_keyboard()
    except Exception:
        return None


def notify_owner_ops(text: str) -> bool:
    """Telegram notify with inline ops keyboard (agent / recover / send)."""
    markup = _owner_ops_markup()
    try:
        from vod_telegram_env import send_message

        return send_message(text, reply_markup=markup)
    except Exception:
        return _send_tg(text)


def maybe_silence_alert(report: HangReport, *, heal: dict | None = None) -> bool:
    alert_sec = max(3600, int(os.environ.get("VOD_SILENCE_ALERT_SEC", "7200")))
    if report.last_send_age_sec is None or report.last_send_age_sec < alert_sec:
        return False
    now = _now()
    state: dict = {}
    if DEFAULT_ALERT_STAMP.is_file():
        try:
            state = json.loads(DEFAULT_ALERT_STAMP.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}
    last = float(state.get("last_alert_ts") or 0)
    # Don't spam: at most once per alert_sec window.
    if now - last < alert_sec:
        return False
    hours = int(report.last_send_age_sec // 3600)
    mins = int((report.last_send_age_sec % 3600) // 60)
    reasons = ", ".join(report.reasons[:4]) or "unknown"
    heal_bit = ""
    if heal:
        heal_bit = f"\nHeal: {heal.get('action')} sent={heal.get('sent', '?')}"
    text = (
        f"⚠️ VOD feed: тишина ~{hours}ч {mins}м\n"
        f"Причины: {reasons}\n"
        f"zero_send_streak={report.zero_send_streak}"
        f"{heal_bit}\n"
        f"Автоагент чинит сейчас. Видео появится только после успешной отправки "
        f"(не путать с heartbeat «scanning»).\n"
        f"Кнопки ниже — только если хочешь форснуть сам."
    )
    if notify_owner_ops(text):
        DEFAULT_ALERT_STAMP.write_text(json.dumps({"last_alert_ts": now}), encoding="utf-8")
        return True
    return False


def run_autonomous_hang_agent(*, game: str = "pubg", force: bool = False) -> dict:
    """Watchdog playbook: clear stale locks → heal → force-send → notify owner.

    Runs without owner button presses. Buttons remain as a manual override.
    """
    cleared = _clear_stale_recover_lock()
    report = detect_hang()
    out: dict = {
        "ok": report.ok,
        "reasons": list(report.reasons),
        "last_send_age_sec": int(report.last_send_age_sec or 0),
        "cleared_stale_lock": cleared,
        "ts": _now(),
    }
    if report.ok and not force:
        out["action"] = "healthy"
        return out

    cooldown_sec = max(900, int(os.environ.get("VOD_HEAL_COOLDOWN_SEC", "2700")))
    if not force and not _heal_cooldown_ok(cooldown_sec):
        out["action"] = "cooldown"
        out["heal"] = {
            "action": "cooldown",
            "reasons": report.reasons,
            "cooldown_sec": cooldown_sec,
        }
        return out

    if not force and _recover_already_running():
        out["action"] = "recover_in_progress"
        out["heal"] = {"action": "recover_in_progress", "reasons": report.reasons}
        return out

    # Prefer full agent playbook on absolute silence / force — not a silent bg recover
    # that can die and leave a ghost lock.
    absolute = max(
        600,
        int(os.environ.get("VOD_ABSOLUTE_SILENCE_SEC", "5400")),
    )
    silence = float(report.last_send_age_sec or 0)
    use_full_agent = (
        force
        or silence >= absolute
        or os.environ.get("VOD_AUTO_HANG_AGENT", "1") == "1"
    )

    if use_full_agent:
        try:
            from telegram_owner_controls import run_hang_agent

            # Background spawn so the 5-min systemd oneshot does not time out.
            if (
                not force
                and os.environ.get("VOD_HEAL_BACKGROUND", "1") == "1"
                and os.environ.get("VOD_AUTO_AGENT_BG", "1") == "1"
            ):
                if _spawn_background_agent(game):
                    _mark_heal("auto_agent_bg", sent=0, escalation=_heal_escalation())
                    out["action"] = "auto_agent_bg"
                    out["heal"] = {"action": "auto_agent_bg"}
                    maybe_silence_alert(report, heal=out["heal"])
                    return out
            msg = run_hang_agent(game)
            sent = 1 if ("SENT" in msg or "отправлен" in msg.lower()) else 0
            import re as _re

            m = _re.search(r"sent[=:\s]+(\d+)", msg, _re.IGNORECASE)
            if m:
                sent = int(m.group(1))
            esc = 0 if sent > 0 else min(2, _heal_escalation() + 1)
            _mark_heal("auto_agent", sent=sent, escalation=esc)
            out["action"] = "auto_agent"
            out["sent"] = sent
            out["report_tail"] = msg.splitlines()[-12:]
            if sent <= 0 or silence >= absolute:
                notify_owner_ops(
                    "🤖 Автоагент зависания отработал:\n"
                    + "\n".join(msg.splitlines()[:18])
                )
            return out
        except Exception as exc:  # noqa: BLE001
            out["agent_error"] = str(exc)

    heal = auto_unload_and_recover(
        report, game=game, force=force or use_full_agent, background=not force
    )
    out["action"] = heal.get("action")
    out["heal"] = heal
    if heal.get("action") not in ("none", "cooldown", "recover_in_progress"):
        maybe_silence_alert(report, heal=heal)
    return out


def _spawn_background_agent(game: str) -> bool:
    """Start hang-agent playbook in background; log to hang_recover_auto.log."""
    if _recover_already_running():
        return False
    _clear_stale_recover_lock()
    script = Path(__file__).resolve()
    log_path = Path(os.environ.get("VOD_AUTO_RECOVER_LOG", str(DEFAULT_AUTO_RECOVER_LOG)))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    esc = _heal_escalation()
    env = apply_agent_recover_env(dict(os.environ), escalation=esc)
    env["VOD_RECOVER_CHILD"] = "1"
    env["VOD_AUTO_AGENT_BG"] = "0"  # child runs synchronously
    header = (
        f"\n===== auto agent {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"game={game} esc={esc} silence={int(last_send_age_sec() or 0)}s =====\n"
    )
    try:
        log_fh = log_path.open("a", encoding="utf-8")
    except OSError:
        log_fh = subprocess.DEVNULL  # type: ignore[assignment]
    if log_fh is not subprocess.DEVNULL:
        try:
            log_fh.write(header)
            log_fh.flush()
        except OSError:
            pass
    proc = subprocess.Popen(
        [sys.executable, "-u", str(script), "--agent", "--game", game],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    try:
        _recover_lock_path().write_text(str(proc.pid), encoding="utf-8")
    except OSError:
        pass
    return True


def run_tick(*, game: str = "pubg", force: bool = False) -> dict:
    report = detect_hang()
    out: dict = {
        "ok": report.ok,
        "reasons": report.reasons,
        "last_send_age_sec": int(report.last_send_age_sec or 0),
        "heartbeat_age_sec": int(report.heartbeat_age_sec or 0) if report.heartbeat_age_sec else None,
        "zero_send_streak": report.zero_send_streak,
        "feed_alive": report.feed_alive,
        "stuck_children": len(report.stuck_children),
        "stuck_parts": len(report.stuck_parts),
        "ts": _now(),
    }
    try:
        DEFAULT_DETECT_STAMP.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_DETECT_STAMP.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    # Always drop ghost locks even when report looks ok — prevents permanent stuck.
    cleared = _clear_stale_recover_lock()
    if cleared:
        out["cleared_stale_lock"] = True
    if not report.ok or force:
        agent = run_autonomous_hang_agent(game=game, force=force)
        out["heal"] = agent.get("heal") or {
            "action": agent.get("action"),
            "sent": agent.get("sent"),
        }
        out["agent"] = {
            "action": agent.get("action"),
            "sent": agent.get("sent"),
            "cleared_stale_lock": agent.get("cleared_stale_lock"),
        }
    return out


def _load_env_file(path: Path) -> None:
    try:
        if not path.is_file():
            return
    except OSError:
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = val.strip().strip('"').strip("'")


def main() -> int:
    env_file = Path(
        os.environ.get("VOD_BOT_ENV_FILE")
        or os.environ.get("VOD_FEED_ENV_FILE")
        or "/root/.video_bot.env"
    )
    _load_env_file(env_file)
    parser = argparse.ArgumentParser(description="VOD hang detector and auto-recover")
    parser.add_argument("--tick", action="store_true", help="Run one watchdog tick (cron)")
    parser.add_argument("--detect", action="store_true", help="Print hang report JSON only")
    parser.add_argument("--recover", action="store_true", help="Force recover now")
    parser.add_argument(
        "--agent",
        action="store_true",
        help="Run full hang-agent playbook (diagnose + recover + force-send + notify)",
    )
    parser.add_argument("--game", default="pubg", choices=("all", *VOD_GAMES))
    args = parser.parse_args()

    if args.agent:
        _clear_stale_recover_lock()
        if not _acquire_recover_lock():
            print(json.dumps({"action": "recover_in_progress"}, ensure_ascii=False))
            return 0
        try:
            from telegram_owner_controls import run_hang_agent

            msg = run_hang_agent(args.game if args.game != "all" else "pubg")
            print(msg)
            notify_owner_ops("🤖 Автоагент зависания:\n" + "\n".join(msg.splitlines()[:18]))
            return 0
        finally:
            _release_recover_lock()

    if args.detect:
        report = detect_hang()
        print(
            json.dumps(
                {
                    "ok": report.ok,
                    "reasons": report.reasons,
                    "last_send_age_sec": report.last_send_age_sec,
                    "heartbeat_age_sec": report.heartbeat_age_sec,
                    "log_age_sec": report.log_age_sec,
                    "zero_send_streak": report.zero_send_streak,
                    "stuck_children": report.stuck_children,
                    "stuck_parts": report.stuck_parts,
                    "feed_alive": report.feed_alive,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.recover:
        report = detect_hang()
        result = auto_unload_and_recover(report, game=args.game, force=True, background=False)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.tick:
        result = run_tick(game=args.game, force=False)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
