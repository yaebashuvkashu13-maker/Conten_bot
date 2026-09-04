#!/usr/bin/env python3
"""Run one synchronous VOD feed cycle (recover / «Отправить сейчас»)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from daily_game_cycle import pubg_only_mode
from vod_feed_recover import clear_feed_locks
from vod_game_registry import VOD_GAMES

SCRIPTS = Path(__file__).resolve().parent
ENV_PATH = Path(os.environ.get("VIDEO_BOT_ENV", "/root/.video_bot.env"))
_PIPELINE_RE = re.compile(
    r"pipeline done sent=(\d+)(?:\s+vods=\d+\s+game=\w+)?(?:\s+(?P<flags>\S+))?"
)
_FEED_PATTERNS: dict[str, tuple[str, ...]] = {
    "mlbb": ("mlbb_vod_segment_feed.py",),
    "pubg": ("shooter_vod_segment_feed.py",),
    "standoff": ("shooter_vod_segment_feed.py",),
    "genshin": ("shooter_vod_segment_feed.py",),
    "wot": ("shooter_vod_segment_feed.py",),
}
_REAL_SUBPROCESS_RUN = subprocess.run


def _systemctl(*args: str) -> None:
    _REAL_SUBPROCESS_RUN(
        ["systemctl", *args],
        check=False,
        timeout=15,
        capture_output=True,
    )


def _target_games(game: str) -> list[str]:
    if game != "all":
        return [game]
    if pubg_only_mode():
        return ["pubg"]
    return list(VOD_GAMES)


def _load_runtime_env() -> dict[str, str]:
    env = {**os.environ}
    try:
        from youtube_download import load_env

        env.update({k: str(v) for k, v in load_env(ENV_PATH).items()})
    except Exception:
        pass
    return env


def _stop_game_feed(game: str) -> None:
    """Stop feed processes by argv match — never use pkill -f (matches SSH/recover wrappers)."""
    patterns = list(_FEED_PATTERNS.get(game, ("shooter_vod_segment_feed.py",)))
    if game != "mlbb" and "shooter_vod_segment_feed.py" not in patterns:
        patterns.append("shooter_vod_segment_feed.py")
    # Also pause supervisor so it cannot respawn mid-send.
    for unit in (
        "content-bot-vod-feed.service",
        "mlbb-vod-feed.service",
    ):
        _systemctl("stop", unit)
    killed = 0
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
        # Match script path anywhere in argv (python -u script.py …).
        if not any(any(pat in arg for pat in patterns) for arg in parts[1:]):
            continue
        if any("vod_force_send.py" in arg for arg in parts):
            continue
        try:
            os.kill(int(pid_name), 9)
            killed += 1
        except OSError:
            pass
    # Clear flock/pid leftovers so the exclusive force-send can start.
    for path in (
        Path(f"/tmp/{game}_vod_segment_feed.lock"),
        Path(f"/tmp/{game}_vod_segment_feed.pid"),
        Path("/tmp/pubg_vod_segment_feed.lock"),
        Path("/tmp/pubg_vod_segment_feed.pid"),
    ):
        path.unlink(missing_ok=True)
    time.sleep(0.6 if killed else 0.2)


def _parse_pipeline_line(line: str) -> dict[str, object]:
    match = _PIPELINE_RE.search(line.strip())
    if not match:
        return {}
    flags = (match.group("flags") or "").strip()
    return {
        "sent": int(match.group(1)),
        "flags": flags,
    }


def _feed_command(game: str) -> list[str]:
    if game == "mlbb":
        return [sys.executable, "-u", str(SCRIPTS / "mlbb_vod_segment_feed.py")]
    return [sys.executable, "-u", str(SCRIPTS / "shooter_vod_segment_feed.py"), game]


def _reject_hint(game: str) -> str:
    try:
        from vod_pipeline_health import health_row

        row = health_row(game)
        reasons = row.get("top_reject_reasons") or {}
        if reasons:
            reason, count = max(reasons.items(), key=lambda item: int(item[1]))
            return f"{reason}×{count}"
        hint = str(row.get("hint") or "").strip()
        if hint and hint != "ok":
            return hint
    except Exception:
        pass
    return ""


def _run_feed_streaming(
    cmd: list[str],
    *,
    env: dict[str, str],
    log_path: Path,
    header: str,
    timeout_sec: int,
) -> tuple[str, int | None, bool]:
    """Run feed with stdout/stderr appended to log in real time.

    Avoids subprocess.capture_output pipe deadlock when the feed is verbose.
    Returns (captured_tail, returncode, timed_out).
    """
    timed_out = False
    returncode: int | None = None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start_offset = 0
    try:
        start_offset = log_path.stat().st_size if log_path.is_file() else 0
    except OSError:
        start_offset = 0
    with log_path.open("a", encoding="utf-8") as log_fh:
        log_fh.write(header)
        log_fh.flush()
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        try:
            returncode = proc.wait(timeout=max(1, int(timeout_sec)))
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            try:
                returncode = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                returncode = proc.returncode
    captured = ""
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as fh:
            fh.seek(max(0, start_offset))
            captured = fh.read()
    except OSError:
        captured = ""
    return captured, returncode, timed_out


def force_send_game(
    game: str,
    *,
    timeout_sec: int | None = None,
    stop_running: bool = True,
) -> dict[str, object]:
    """Clear locks and run one feed iteration; return sent count and diagnostics."""
    game = game.strip().lower()
    if game not in VOD_GAMES:
        return {"game": game, "sent": 0, "error": f"unknown game {game!r}"}

    if timeout_sec is None:
        timeout_sec = max(120, int(os.environ.get("VOD_FORCE_SEND_TIMEOUT_SEC", "900")))

    if stop_running:
        _stop_game_feed(game)
    clear_feed_locks()

    env = _load_runtime_env()
    env["VOD_SEGMENT_GAME"] = game
    scripts_path = str(SCRIPTS)
    local_bin = "/usr/local/bin"
    py_path = env.get("PYTHONPATH", "")
    parts = [p for p in py_path.split(":") if p]
    for path in (scripts_path, local_bin):
        if path not in parts:
            parts.insert(0, path)
    env["PYTHONPATH"] = ":".join(parts)
    env.setdefault("SHOOTER_VOD_MAX_VODS_PER_RUN", os.environ.get("VOD_FORCE_SEND_MAX_VODS", "1"))
    env["VOD_ZERO_SEND_COOLDOWN_SEC"] = "0"
    # Recover/force-send must re-scan inbox immediately.
    env["VOD_ZERO_SEND_COOLDOWN_SEC"] = "0"
    env["SHOOTER_VOD_SCAN_COOLDOWN_SEC"] = os.environ.get("VOD_FORCE_SCAN_COOLDOWN_SEC", "60")
    from vod_feed_recover import bump_scan_cooldowns, unpark_ready_vods

    unpark_ready_vods(game, limit=max(1, int(os.environ.get("VOD_RECOVER_UNPARK", "3"))))
    bump_scan_cooldowns(game)
    if game == "pubg":
        env.setdefault("PUBG_VOD_SINGLES_FIRST", "1")
        env["PUBG_SINGLES_ZERO_SEND_EXHAUST"] = os.environ.get("VOD_FORCE_SEND_ZERO_EXHAUST", "6")
        env.setdefault("PUBG_SINGLES_MAX_VODS_PER_RUN", os.environ.get("VOD_FORCE_SEND_MAX_VODS", "4"))
        env["VOD_ZERO_SEND_COOLDOWN_SEC"] = "0"
        try:
            from pubg_owner_calibration import apply_owner_send_policy

            apply_owner_send_policy()
            for key in (
                "PUBG_PRESEND_SHOOTING_GATE",
                "PUBG_PRESEND_SCORE_MODE",
                "PUBG_REJECT_LOOT_WALK",
                "PUBG_FAST_RANK_DROP_LOOT_WALK",
                "PUBG_EARLY_PAYOFF_REJECT_SINGLES",
                "PUBG_PAYOFF_SCORE_MIN_SINGLES",
                "PUBG_QUALITY_SCORE_MIN_SINGLES",
                "PUBG_SINGLES_GUN_PAYOFF_BYPASS",
                "PUBG_SINGLES_GUN_QUALITY_BYPASS",
                "PUBG_SINGLE_MIN_GUN_DENSITY",
                "PUBG_CLIP_MIN_BURST_RATIO",
                "SHOOTER_VOD_MONTAGE_SHOOTING_ONLY",
            ):
                if key in os.environ:
                    env[key] = os.environ[key]
        except ImportError:
            env.setdefault("PUBG_EARLY_PAYOFF_REJECT_SINGLES", "0")
            env.setdefault("PUBG_PAYOFF_SCORE_MIN_SINGLES", "0.10")
            env.setdefault("PUBG_QUALITY_SCORE_MIN_SINGLES", "0.28")
            env.setdefault("PUBG_SINGLES_GUN_PAYOFF_BYPASS", "1")
            env.setdefault("PUBG_SINGLES_GUN_QUALITY_BYPASS", "1")
            env.setdefault("PUBG_SINGLE_MIN_GUN_DENSITY", "0.045")
        # Long silence: soften singles gates so recover can ship *something*
        # instead of burning cooldown on endless zero-sends.
        drought = False
        try:
            from vod_hang_detector import last_send_age_sec

            drought = float(last_send_age_sec() or 0) >= float(
                os.environ.get("VOD_FORCE_DROUGHT_SEC", "7200")
            )
        except Exception:
            drought = False
        if drought or os.environ.get("VOD_FORCE_SOFTEN", "0") == "1":
            env["PUBG_PRESEND_SHOOTING_GATE"] = os.environ.get("VOD_FORCE_PRESEND_GATE", "0")
            env["PUBG_EARLY_PAYOFF_REJECT_SINGLES"] = "0"
            env["PUBG_PAYOFF_SCORE_MIN_SINGLES"] = os.environ.get(
                "VOD_FORCE_PAYOFF_MIN", "0.08"
            )
            env["PUBG_QUALITY_SCORE_MIN_SINGLES"] = os.environ.get(
                "VOD_FORCE_QUALITY_MIN", "0.22"
            )
            env["PUBG_SINGLES_GUN_PAYOFF_BYPASS"] = "1"
            env["PUBG_SINGLES_GUN_QUALITY_BYPASS"] = "1"
            env["PUBG_SINGLE_MIN_GUN_DENSITY"] = os.environ.get(
                "VOD_FORCE_GUN_DENSITY", "0.030"
            )
            env["PUBG_CLIP_MIN_BURST_RATIO"] = os.environ.get(
                "VOD_FORCE_BURST_RATIO", "3.5"
            )
            env["PUBG_REJECT_LOOT_WALK"] = os.environ.get("VOD_FORCE_REJECT_LOOT", "0")
            env["SHOOTER_VOD_MAX_VODS_PER_RUN"] = os.environ.get(
                "VOD_FORCE_SEND_MAX_VODS", "4"
            )
            env["PUBG_SINGLES_MAX_VODS_PER_RUN"] = env["SHOOTER_VOD_MAX_VODS_PER_RUN"]
            env["PUBG_SINGLES_ZERO_SEND_EXHAUST"] = os.environ.get(
                "VOD_FORCE_SEND_ZERO_EXHAUST", "12"
            )
            # Stay on unparked inbox — Metro live discovery burns the whole timeout.
            env["SHOOTER_VOD_SKIP_DISCOVERY"] = os.environ.get(
                "VOD_FORCE_SKIP_DISCOVERY", "1"
            )

    log_path = Path(os.environ.get("VOD_FORCE_SEND_LOG", "/root/data/mlbb/force_send_now.log"))
    captured = ""
    returncode: int | None = None
    timed_out = False
    try:
        header = f"\n===== force_send {game} {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n"
        # Stream stdout to the log as it arrives. capture_output=True deadlocks /
        # blinds recover when the feed prints more than a pipe buffer (~64KiB).
        captured, returncode, timed_out = _run_feed_streaming(
            _feed_command(game),
            env=env,
            log_path=log_path,
            header=header,
            timeout_sec=timeout_sec,
        )
    except Exception as exc:
        return {
            "game": game,
            "sent": 0,
            "error": str(exc)[:240],
            "hint": _reject_hint(game),
        }
    finally:
        # Resume supervisor after exclusive send (best-effort).
        for unit in ("content-bot-vod-feed.service", "mlbb-vod-feed.service"):
            _systemctl("start", unit)

    if timed_out:
        return {
            "game": game,
            "sent": 0,
            "error": f"timeout {timeout_sec}s",
            "hint": _reject_hint(game),
        }

    sent = 0
    flags = ""
    text = captured[-50000:] if captured else ""
    if not text:
        try:
            text = log_path.read_text(encoding="utf-8", errors="ignore")[-50000:]
        except OSError:
            text = ""
    for line in text.splitlines():
        parsed = _parse_pipeline_line(line)
        if parsed:
            sent = int(parsed.get("sent") or 0)
            flags = str(parsed.get("flags") or "")

    hint = ""
    if sent <= 0:
        hint = _reject_hint(game)
        if not hint and flags:
            hint = flags.replace("=", " ")

    err_tail = ""
    if returncode not in (0, None) and sent <= 0:
        err_tail = (text[-240:] if text else "").strip()

    return {
        "game": game,
        "sent": sent,
        "returncode": returncode,
        "flags": flags,
        "hint": hint,
        "error": err_tail or None,
    }


def force_send(game: str = "all") -> list[dict[str, object]]:
    return [force_send_game(g) for g in _target_games(game)]


def format_force_send_report(results: list[dict[str, object]]) -> str:
    lines = ["📤 Принудительная отправка"]
    any_sent = False
    for row in results:
        g = str(row.get("game") or "?").upper()
        sent = int(row.get("sent") or 0)
        if sent > 0:
            any_sent = True
            lines.append(f"• {g}: отправлено {sent} ✅")
            continue
        hint = str(row.get("hint") or "").strip()
        err = str(row.get("error") or "").strip()
        flags = str(row.get("flags") or "").strip()
        detail = hint or flags or err or "0 клипов прошли гейты"
        lines.append(f"• {g}: не отправлено — {detail}")
    if any_sent:
        lines.append("Клип(ы) должны прийти в этот чат в течение ~1 мин.")
    else:
        lines.append("Если снова тишина — /reset pubg и повтори Recover.")
    return "\n".join(lines)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Force one VOD feed send cycle")
    parser.add_argument("--game", default="all", choices=("all", *VOD_GAMES))
    args = parser.parse_args()
    results = force_send(args.game)
    print(format_force_send_report(results))
    return 0 if any(int(r.get("sent") or 0) > 0 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
