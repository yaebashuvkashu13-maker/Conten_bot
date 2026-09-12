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


def _recover_in_progress() -> bool:
    """True while hang --recover / exclusive force-send holds the lock."""
    lock = Path(os.environ.get("VOD_HANG_RECOVER_LOCK", "/tmp/vod_hang_recover.lock"))
    if not lock.is_file():
        return False
    try:
        pid = int(lock.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        try:
            lock.unlink()
        except OSError:
            pass
        return False


def _pgrep(pattern: str) -> list[int]:
    """Match real argv tokens only — never pgrep -f (matches SSH/bash wrappers)."""
    needle = pattern.replace("\\", "").replace(r"\.", ".")
    # Accept either regex-ish or plain substring of the script name.
    plain = needle.replace(".*", "").replace("^", "").replace("$", "")
    found: list[int] = []
    for pid_name in os.listdir("/proc"):
        if not pid_name.isdigit():
            continue
        try:
            raw = Path(f"/proc/{pid_name}/cmdline").read_bytes()
        except OSError:
            continue
        parts = [p.decode(errors="ignore") for p in raw.split(b"\0") if p]
        if len(parts) < 2:
            continue
        # Skip remote shells that merely mention the script in -c payloads.
        if parts[0].endswith("bash") or parts[0].endswith("sh"):
            if any(a in ("-c", "-lc") for a in parts[1:3]):
                continue
        if any(plain in arg for arg in parts):
            found.append(int(pid_name))
    return found


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
    if _recover_in_progress():
        return {"skipped": True, "reason": "recover_in_progress"}
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


def maybe_heal_unit(*, active: str, ledger_age: float | None, silence_limit: float) -> dict:
    """Soft recover: restart dead unit; escalate hang oneshot on ledger silence."""
    out: dict = {"unit_restart": False, "hang_tick": False}
    if _recover_in_progress():
        return {**out, "skipped": True, "reason": "recover_in_progress"}
    if _env("VOD_FEED_AUTO_HEAL_UNIT", "1") == "1" and active not in {"active", "activating"}:
        _systemctl("reset-failed", SERVICE)
        _systemctl("start", SERVICE)
        out["unit_restart"] = True
        time.sleep(2)
        out["active_after"] = _systemctl("is-active", SERVICE) or "unknown"
    silence = ledger_age is None or (silence_limit > 0 and ledger_age > silence_limit)
    if silence and _env("VOD_FEED_AUTO_HANG_ON_SILENCE", "1") == "1":
        hang_unit = _env("VOD_HANG_SYSTEMD_UNIT", "content-bot-vod-hang.service")
        out["hang_unit"] = hang_unit
        try:
            proc = subprocess.run(
                ["systemctl", "start", hang_unit],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            out["hang_tick"] = proc.returncode == 0
            if proc.returncode != 0:
                out["hang_err"] = (proc.stderr or proc.stdout or "").strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            out["hang_err"] = str(exc)
        if not out["hang_tick"]:
            try:
                from vod_hang_detector import run_tick

                out["hang_inline"] = run_tick(
                    game=_env("VOD_FEED_HEALTH_GAME", "pubg"), force=False
                )
                out["hang_tick"] = True
            except Exception as exc2:  # noqa: BLE001
                out["hang_inline_err"] = str(exc2)
    return out


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
    silence_limit = max(0.0, float(args.ledger_silence_hours)) * 3600.0
    soft = maybe_heal_unit(active=active, ledger_age=ledger_age, silence_limit=silence_limit)
    if soft.get("unit_restart"):
        active = soft.get("active_after") or _systemctl("is-active", SERVICE) or active
        supers = _pgrep("mlbb_vod_segment_feed\\.sh")
        feeds = _pgrep("shooter_vod_segment_feed\\.py")
    heal = {**heal, "soft": soft}

    problems: list[str] = []
    if active not in {"active", "activating"}:
        problems.append(f"unit={active}")
    if restarts >= args.restart_storm:
        problems.append(f"restart_storm NRestarts={restarts}")
    if len(supers) > 1:
        problems.append(f"duplicate_supervisors={len(supers)} heal={heal}")
    if not feeds and active == "active":
        problems.append("no shooter_vod_segment_feed.py while unit active")
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
