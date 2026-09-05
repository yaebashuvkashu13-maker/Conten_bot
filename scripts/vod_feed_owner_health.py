#!/usr/bin/env python3
"""Single-owner feed healthcheck: restart storms, duplicate supervisors, silent ledger."""

from __future__ import annotations

import argparse
import calendar
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SERVICE = os.environ.get("VOD_FEED_SYSTEMD_UNIT", "content-bot-vod-feed.service")
STATE_PATH = Path(
    os.environ.get("VOD_FEED_HEALTH_STATE", "/root/data/vod_feed_health.json")
)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _load_env_file(path: Path) -> None:
    try:
        if not path.exists():
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


def telegram_send(text: str) -> bool:
    try:
        from vod_telegram_env import send_message

        return send_message(text)
    except Exception:
        return False



def _systemctl(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["systemctl", *args], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""


def _pgrep(pattern: str) -> list[int]:
    try:
        out = subprocess.check_output(["pgrep", "-f", pattern], text=True)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []
    return [int(x) for x in out.splitlines() if x.strip().isdigit()]


def n_restarts() -> int:
    raw = _systemctl("show", SERVICE, "-p", "NRestarts", "--value")
    try:
        return int(raw or 0)
    except ValueError:
        return 0


def ledger_gate_age_sec(game: str) -> float | None:
    """Age of newest reject/sent/heartbeat ledger event (seconds), or None."""
    try:
        from vod_clip_quality_ledger import latest_gate_event_age_sec

        return latest_gate_event_age_sec(game)
    except Exception:
        return None



def maybe_heal_duplicates() -> dict:
    """Keep systemd as sole owner: kill orphan supervisors outside the unit."""
    if _env("VOD_FEED_AUTO_HEAL_DUPES", "1") != "1":
        return {"skipped": True}
    pids = _pgrep("mlbb_vod_segment_feed\\.sh")
    if len(pids) <= 1:
        return {"supervisors": len(pids), "killed": []}
    try:
        main_pid = int(_systemctl("show", SERVICE, "-p", "MainPID", "--value") or 0)
    except ValueError:
        main_pid = 0
    keep = main_pid if main_pid in pids else min(pids)
    killed: list[int] = []
    for pid in pids:
        if pid == keep:
            continue
        try:
            os.kill(pid, 15)
            killed.append(pid)
        except OSError:
            continue
    if killed:
        time.sleep(2)
        _systemctl("start", SERVICE)
    return {"supervisors": len(pids), "kept": keep, "killed": killed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--game", default=_env("VOD_FEED_HEALTH_GAME", "pubg"))
    ap.add_argument(
        "--env-file", default=_env("VOD_FEED_ENV_FILE", "/root/.video_bot.env")
    )
    ap.add_argument(
        "--restart-storm",
        type=int,
        default=int(_env("VOD_FEED_RESTART_STORM", "12") or 12),
    )
    ap.add_argument(
        "--ledger-silence-hours",
        type=float,
        default=float(_env("VOD_LEDGER_SILENCE_HOURS", "3") or 3),
    )
    args = ap.parse_args(argv)
    _load_env_file(Path(args.env_file))

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    prev: dict = {}
    if STATE_PATH.exists():
        try:
            prev = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prev = {}

    active = _systemctl("is-active", SERVICE) or "unknown"
    restarts = n_restarts()
    supers = _pgrep("mlbb_vod_segment_feed\\.sh")
    feeds = _pgrep("shooter_vod_segment_feed\\.py")
    heal = maybe_heal_duplicates() if len(supers) > 1 else {"supervisors": len(supers)}
    ledger_age = ledger_gate_age_sec(args.game)

    problems: list[str] = []
    if active not in {"active", "activating"}:
        problems.append(f"unit={active}")
    if restarts >= args.restart_storm:
        problems.append(f"restart_storm NRestarts={restarts}")
    if len(supers) > 1:
        problems.append(f"duplicate_supervisors={len(supers)} heal={heal}")
    if not feeds and active == "active":
        problems.append("no shooter_vod_segment_feed.py while unit active")
    silence_limit = max(0.0, float(args.ledger_silence_hours)) * 3600.0
    if ledger_age is None:
        problems.append("ledger_no_reject_sent_or_heartbeat")
    elif ledger_age > silence_limit:
        problems.append(f"ledger_silent={ledger_age / 3600:.1f}h")

    report = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game": args.game,
        "unit": SERVICE,
        "active": active,
        "n_restarts": restarts,
        "supervisors": len(supers),
        "feed_pids": feeds,
        "ledger_age_sec": ledger_age,
        "heal": heal,
        "problems": problems,
        "status": "ok" if not problems else "degraded",
    }

    last_alert = float(prev.get("last_alert_ts") or 0)
    cooldown = float(_env("VOD_FEED_HEALTH_ALERT_COOLDOWN_SEC", "1800") or 1800)
    try:
        from vod_telegram_env import credentials_ok

        tg_ok = credentials_ok()
    except Exception:
        tg_ok = False
    report["telegram_creds_ok"] = tg_ok
    if problems and (time.time() - last_alert) >= cooldown:
        text = (
            f"⚠️ VOD feed health [{args.game}]\n"
            + "\n".join(f"• {p}" for p in problems)
            + f"\nunit={active} restarts={restarts} supers={len(supers)} feeds={len(feeds)}"
        )
        if not tg_ok:
            report["alerted"] = False
            report["alert_error"] = "missing_telegram_creds"
            problems.append("cannot_page_missing_TG_BOT_TOKEN_or_TG_CHAT_ID")
            report["problems"] = problems
            report["status"] = "degraded"
        else:
            report["alerted"] = telegram_send(text)
            if not report["alerted"]:
                report["alert_error"] = "telegram_send_failed"
        report["last_alert_ts"] = time.time()
    else:
        report["alerted"] = False
        report["last_alert_ts"] = last_alert

    STATE_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
