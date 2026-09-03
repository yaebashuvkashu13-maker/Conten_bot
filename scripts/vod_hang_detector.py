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
    restart_supervisor,
    run_recover,
    unpark_ready_vods,
)
from vod_game_registry import VOD_GAMES, inbox_video_ids, load_state, save_state, spec

DEFAULT_HEARTBEAT = Path("/root/data/mlbb/vod_feed_heartbeat.json")
DEFAULT_HEAL_STAMP = Path("/root/data/mlbb/vod_auto_heal.json")
DEFAULT_ALERT_STAMP = Path("/root/data/mlbb/vod_silence_alert.json")
SEND_LINE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?"
    r"(?:pipeline done sent=([1-9]\d*)|PUBG sent=([1-9]\d*)|sent=([1-9]\d*) vods=1)",
)
PIPELINE_DONE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*pipeline done sent=(\d+)"
)
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
        if sent_path.is_file():
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
    if log_path.is_file():
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


def zero_send_streak(max_lines: int = 8000) -> int:
    log_path = feed_log_path()
    if not log_path.is_file():
        return 0
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]
    except OSError:
        return 0
    streak = 0
    for line in reversed(lines):
        m = PIPELINE_DONE_RE.search(line)
        if not m:
            continue
        sent = int(m.group(2))
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
        if "metro_reject=1" not in line and "sent=0" not in line:
            continue
        for mp4 in mp4s:
            vid = mp4.stem[3:][:11] if mp4.stem.startswith("yt_") else mp4.stem[:11]
            if vid and vid in line:
                reject_counts[vid] = reject_counts.get(vid, 0) + 1

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
    silence_heal = max(silence_warn, int(os.environ.get("VOD_SILENCE_HEAL_SEC", "5400")))
    progress_stuck = max(300, int(os.environ.get("VOD_PROGRESS_STUCK_SEC", "900")))
    zero_streak_heal = max(3, int(os.environ.get("VOD_ZERO_SEND_STREAK_HEAL", "6")))

    if report.last_send_age_sec is not None and report.last_send_age_sec >= silence_warn:
        report.add(f"silence_{int(report.last_send_age_sec)}s")

    if report.zero_send_streak >= zero_streak_heal:
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

    return report


def _heal_cooldown_ok(min_sec: int | None = None) -> bool:
    min_sec = min_sec or max(900, int(os.environ.get("VOD_HEAL_COOLDOWN_SEC", "2700")))
    stamp = DEFAULT_HEAL_STAMP
    if not stamp.is_file():
        return True
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
        last = float(data.get("last_heal_ts") or 0)
    except (json.JSONDecodeError, OSError, ValueError):
        return True
    return (_now() - last) >= min_sec


def _mark_heal(action: str) -> None:
    DEFAULT_HEAL_STAMP.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_HEAL_STAMP.write_text(
        json.dumps({"last_heal_ts": _now(), "action": action}),
        encoding="utf-8",
    )


def _send_tg(text: str) -> bool:
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT_ID", "").strip()
    if not token or not chat:
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            timeout=20,
        )
        return True
    except Exception:
        return False


def _recover_lock_path() -> Path:
    return Path(os.environ.get("VOD_HANG_RECOVER_LOCK", "/tmp/vod_hang_recover.lock"))


def _recover_already_running() -> bool:
    """True if a previous --recover / force_send is still alive."""
    lock = _recover_lock_path()
    if not lock.is_file():
        return False
    try:
        pid = int(lock.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        lock.unlink(missing_ok=True)
        return False


def _acquire_recover_lock() -> bool:
    if _recover_already_running():
        return False
    path = _recover_lock_path()
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
        subprocess.run(
            ["systemctl", "start", unit],
            check=False,
            timeout=20,
            capture_output=True,
        )


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

    for g in ([game] if game != "all" else list(VOD_GAMES)):
        clear_discovery_pauses(g)
        bump_scan_cooldowns(g)
        park_exhausted_inbox(g)
        unparked = unpark_ready_vods(g, limit=max(2, int(os.environ.get("VOD_RECOVER_UNPARK", "4"))))
        if unparked:
            actions.append(f"unpark_{g}={unparked}")

    silence_heal = max(3600, int(os.environ.get("VOD_SILENCE_HEAL_SEC", "5400")))
    need_full_recover = force or (
        report.last_send_age_sec is not None and report.last_send_age_sec >= silence_heal
    ) or report.zero_send_streak >= int(os.environ.get("VOD_ZERO_SEND_STREAK_HEAL", "6"))

    if need_full_recover:
        # Mark heal FIRST so concurrent cron ticks see cooldown immediately.
        _mark_heal("full_recover_pending")
        clear_stale_owner_batch_lock()
        clear_feed_locks()
        if background and os.environ.get("VOD_HEAL_BACKGROUND", "1") == "1":
            if _recover_already_running():
                return {"action": "recover_in_progress", "actions": actions, "reasons": report.reasons}
            script = Path(__file__).resolve()
            subprocess.Popen(
                [sys.executable, str(script), "--recover", "--game", game],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            actions.append("full_recover_bg")
            _mark_heal("full_recover_bg")
            return {
                "action": "full_recover_bg",
                "actions": actions,
                "reasons": report.reasons,
            }
        if not _acquire_recover_lock():
            return {"action": "recover_in_progress", "actions": actions, "reasons": report.reasons}
        try:
            # Prefer inbox scan: only stop competing feeds, then force_send once.
            stop_feed_processes(game)
            msg = run_recover(game, force_send=True)
            _start_systemd_feed()
            restarted, _ = restart_supervisor(force=True)
            actions.append("full_recover")
            if restarted:
                actions.append("supervisor_restarted")
            _mark_heal("full_recover")
            return {
                "action": "full_recover",
                "actions": actions,
                "reasons": report.reasons,
                "recover_tail": msg.splitlines()[-6:],
            }
        finally:
            _release_recover_lock()

    # Light heal: restart feed only — never spam this either.
    _mark_heal("light_restart")
    clear_feed_locks()
    stop_feed_processes(game)
    _start_systemd_feed()
    restarted, note = restart_supervisor(force=True)
    actions.append(f"light_restart:{note}")
    return {"action": "light_restart", "actions": actions, "reasons": report.reasons}


def maybe_silence_alert(report: HangReport) -> bool:
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
    text = (
        f"⚠️ VOD feed: тишина ~{hours}ч {mins}м\n"
        f"Причины: {reasons}\n"
        f"zero_send_streak={report.zero_send_streak}\n"
        f"Автовосстановление запущено (следующее не раньше чем через "
        f"{int(os.environ.get('VOD_HEAL_COOLDOWN_SEC', '2700')) // 60} мин)."
    )
    if _send_tg(text):
        DEFAULT_ALERT_STAMP.write_text(json.dumps({"last_alert_ts": now}), encoding="utf-8")
        return True
    return False


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
    }
    if not report.ok or force:
        # Alert only when we actually heal (not on cooldown spam).
        heal = auto_unload_and_recover(report, game=game, force=force, background=not force)
        out["heal"] = heal
        if heal.get("action") not in ("none", "cooldown", "recover_in_progress"):
            maybe_silence_alert(report)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="VOD hang detector and auto-recover")
    parser.add_argument("--tick", action="store_true", help="Run one watchdog tick (cron)")
    parser.add_argument("--detect", action="store_true", help="Print hang report JSON only")
    parser.add_argument("--recover", action="store_true", help="Force recover now")
    parser.add_argument("--game", default="pubg", choices=("all", *VOD_GAMES))
    args = parser.parse_args()

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
