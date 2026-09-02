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
    patterns = list(_FEED_PATTERNS.get(game, ("shooter_vod_segment_feed.py",)))
    if game != "mlbb" and "shooter_vod_segment_feed.py" not in patterns:
        patterns.append("shooter_vod_segment_feed.py")
    for pat in patterns:
        subprocess.run(["pkill", "-9", "-f", pat], check=False, timeout=5)
    time.sleep(0.4)


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
    env.setdefault("SHOOTER_VOD_MAX_VODS_PER_RUN", os.environ.get("VOD_FORCE_SEND_MAX_VODS", "1"))
    env["VOD_ZERO_SEND_COOLDOWN_SEC"] = "0"
    if game == "pubg":
        env.setdefault("PUBG_VOD_SINGLES_FIRST", "1")
        env["PUBG_SINGLES_ZERO_SEND_EXHAUST"] = os.environ.get("VOD_FORCE_SEND_ZERO_EXHAUST", "4")

    try:
        proc = subprocess.run(
            _feed_command(game),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "game": game,
            "sent": 0,
            "error": f"timeout {timeout_sec}s",
            "hint": _reject_hint(game),
        }

    sent = 0
    flags = ""
    for line in (proc.stdout or "").splitlines():
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
    if proc.returncode not in (0, None) and sent <= 0:
        err_tail = ((proc.stderr or proc.stdout or "")[-240:]).strip()

    return {
        "game": game,
        "sent": sent,
        "returncode": proc.returncode,
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
