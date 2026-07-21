#!/usr/bin/env python3
"""Recover stuck daily VOD cycle: wrong-game feed, zero-send loops, hung runner."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

ENV_PATH = Path(os.environ.get("ENV_FILE", "/root/.video_bot.env"))
FEED_LOG = Path(os.environ.get("MLBB_VOD_FEED_LOG", "/root/data/mlbb/mlbb_vod_segment_feed.log"))
STATE_PATH = Path(os.environ.get("DAILY_GAME_CYCLE_STATE", "/root/data/mlbb/daily_game_cycle.json"))
ALERT_STATE = Path("/root/data/mlbb/vod_cycle_watchdog_alert.json")

ZERO_SEND_RE = re.compile(
    r"zero send — keep vod=(?P<vod>\S+) for retry \(presend/soften\) streak=(?P<streak>\d+)"
)
CYCLE_BLOCK_RE = re.compile(r"send blocked seg=\S+ cycle=(?P<reason>\S+)")
RUNNER_STUCK_SEC = int(os.environ.get("VOD_CYCLE_RUNNER_STUCK_SEC", "600"))
ZERO_LOOP_STREAK = int(os.environ.get("VOD_CYCLE_ZERO_LOOP_STREAK", "8"))
ALERT_COOLDOWN_SEC = int(os.environ.get("VOD_CYCLE_ALERT_COOLDOWN_SEC", "7200"))


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if v})
    return env


def _pgrep(pattern: str) -> list[int]:
    proc = subprocess.run(
        ["pgrep", "-f", pattern],
        capture_output=True,
        text=True,
        check=False,
    )
    out: list[int] = []
    for line in (proc.stdout or "").split():
        try:
            out.append(int(line.strip()))
        except ValueError:
            continue
    return out


def _proc_age_sec(pid: int) -> float:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().split()
        start_ticks = int(stat[21])
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        return max(0.0, uptime - start_ticks / hz)
    except (OSError, ValueError, IndexError):
        return -1.0


def _log_mtime_age() -> float:
    if not FEED_LOG.exists():
        return 1e9
    return max(0.0, time.time() - FEED_LOG.stat().st_mtime)


def _tail_lines(limit: int = 400) -> list[str]:
    if not FEED_LOG.exists():
        return []
    try:
        data = FEED_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-limit:]


def _zero_send_loop(lines: list[str]) -> tuple[bool, str]:
    """Same VOD retried with high zero-send streak → likely quota/handoff stall."""
    last_vod = ""
    streak = 0
    for line in reversed(lines):
        m = ZERO_SEND_RE.search(line)
        if not m:
            continue
        vod = m.group("vod")
        n = int(m.group("streak"))
        if last_vod and vod != last_vod:
            break
        last_vod = vod
        streak = max(streak, n)
        if streak >= ZERO_LOOP_STREAK:
            return True, f"zero_send_loop vod={last_vod} streak={streak}"
    return False, ""


def _recent_cycle_block(lines: list[str]) -> str:
    for line in reversed(lines):
        m = CYCLE_BLOCK_RE.search(line)
        if m:
            return m.group("reason")
    return ""


def _kill_patterns(patterns: list[str]) -> list[str]:
    killed: list[str] = []
    for pattern in patterns:
        for pid in _pgrep(pattern):
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(f"{pattern}:pid={pid}")
            except OSError:
                continue
    if killed:
        time.sleep(2)
        for pattern in patterns:
            for pid in _pgrep(pattern):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    return killed


def _maybe_alert(token: str, chat_id: str, message: str) -> None:
    now = time.time()
    state: dict = {}
    if ALERT_STATE.exists():
        try:
            state = json.loads(ALERT_STATE.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    last = float(state.get("last_alert_ts") or 0)
    if now - last < ALERT_COOLDOWN_SEC:
        return
    try:
        import urllib.parse
        import urllib.request

        data = urllib.parse.urlencode({"chat_id": chat_id, "text": message[:3900]}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            timeout=20,
        )
        ALERT_STATE.write_text(json.dumps({"last_alert_ts": now, "message": message[:500]}), encoding="utf-8")
    except Exception:
        pass


def diagnose() -> dict:
    env = _load_env()
    lines = _tail_lines()
    report: dict = {
        "cycle_enabled": env.get("DAILY_GAME_CYCLE_ENABLED", "0") == "1",
        "issues": [],
        "actions": [],
    }
    if not report["cycle_enabled"]:
        return report

    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from daily_game_cycle import active_game, can_send_for_game, status_summary

    active = active_game()
    report["active_game"] = active
    report["remaining"] = status_summary().get("remaining", {})

    mlbb_pids = _pgrep("mlbb_vod_segment_feed.py")
    shooter_pids: list[int] = []
    for game in ("pubg", "standoff", "genshin", "wot"):
        shooter_pids.extend(_pgrep(f"shooter_vod_segment_feed.py {game}"))
    runner_pids = _pgrep("daily_cycle_runner.py")

    if active and active != "mlbb" and mlbb_pids:
        report["issues"].append(f"mlbb_feed_running_while_active={active}")
        report["actions"].append("kill_mlbb_feed")

    if active and active != "mlbb":
        expected = f"shooter_vod_segment_feed.py {active}"
        has_expected = bool(_pgrep(expected))
        log_age = _log_mtime_age()
        if not has_expected and runner_pids and log_age > 300:
            report["issues"].append(f"missing_{active}_feed log_age={int(log_age)}s")
            report["actions"].append("kill_runner")

    loop, loop_detail = _zero_send_loop(lines)
    if loop:
        report["issues"].append(loop_detail)
        cycle_reason = _recent_cycle_block(lines)
        if cycle_reason.startswith("wait_for") or cycle_reason.endswith("_quota_done"):
            report["actions"].append("kill_mlbb_feed")
        else:
            report["actions"].append("kill_feed")

    for pid in runner_pids:
        age = _proc_age_sec(pid)
        if age < RUNNER_STUCK_SEC:
            continue
        children = _pgrep("mlbb_vod_segment_feed.py") + shooter_pids
        if not children and _log_mtime_age() > 300:
            report["issues"].append(f"runner_stuck pid={pid} age={int(age)}s")
            report["actions"].append("kill_runner")

    ok_mlbb, mlbb_reason = can_send_for_game("mlbb", 1)
    if not ok_mlbb and mlbb_pids and mlbb_reason.startswith("wait_for"):
        report["issues"].append(f"mlbb_blocked_{mlbb_reason}")
        if "kill_mlbb_feed" not in report["actions"]:
            report["actions"].append("kill_mlbb_feed")

    return report


def recover(*, dry_run: bool = False, notify: bool = True) -> dict:
    report = diagnose()
    actions = list(dict.fromkeys(report.get("actions") or []))
    killed: list[str] = []

    if dry_run:
        report["dry_run"] = True
        report["would_kill"] = actions
        return report

    if "kill_mlbb_feed" in actions:
        killed.extend(
            _kill_patterns(
                [
                    "mlbb_vod_segment_feed.py",
                ]
            )
        )
    if "kill_runner" in actions or "kill_feed" in actions:
        killed.extend(
            _kill_patterns(
                [
                    "daily_cycle_runner.py",
                    "mlbb_vod_segment_feed.py",
                    "shooter_vod_segment_feed.py",
                ]
            )
        )
        for lock in Path("/tmp").glob("*_vod_segment_feed.lock"):
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass

    report["killed"] = killed
    if killed and notify:
        env = _load_env()
        token = env.get("TG_BOT_TOKEN", "").strip()
        chat_id = env.get("TG_CHAT_ID", "").strip()
        if token and chat_id:
            issues = "; ".join(report.get("issues") or [])
            _maybe_alert(
                token,
                chat_id,
                "🔧 VOD cycle watchdog: перезапуск пайплайна\n"
                f"Причины: {issues}\n"
                f"Действия: {', '.join(actions)}",
            )
    return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Daily VOD cycle stuck-feed recovery")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = recover(dry_run=args.dry_run, notify=not args.no_notify)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result.get("issues") else 1


if __name__ == "__main__":
    raise SystemExit(main())
