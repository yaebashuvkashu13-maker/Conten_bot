#!/usr/bin/env python3
"""After daily quotas fill: send 1 highlight montage per game (once per day).

Enabled by POST_QUOTA_MONTAGE=1 (default when daily cycle is on).
Does not burn daily single-clip quotas. Each montage prefers a fresh VOD
and never reuses prior montage peaks (see montage_dedup).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from daily_game_cycle import GAME_ORDER, _today_key, status_summary
from montage_dedup import day_montage_done, mark_montage_sent

log = logging.getLogger("post_quota_montages")

SCRIPTS = Path(__file__).resolve().parent
ENV_PATH = Path(os.environ.get("VIDEO_BOT_ENV", "/root/.video_bot.env"))


def post_quota_enabled() -> bool:
    raw = os.environ.get("POST_QUOTA_MONTAGE")
    if raw is None or str(raw).strip() == "":
        # Default ON whenever the daily cycle is active.
        return os.environ.get("DAILY_GAME_CYCLE_ENABLED", "0") == "1"
    return str(raw).strip() not in {"0", "false", "False", "no"}


def montage_only_mode() -> bool:
    """Future switch: skip single clips, ship only montages. Off until quotas are stable."""
    return os.environ.get("MONTAGE_ONLY_MODE", "0") == "1"


def games_pending_today() -> list[str]:
    day = _today_key()
    return [g for g in GAME_ORDER if not day_montage_done(g, day)]


def _notify(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        return
    try:
        from mlbb_vod_segment_feed import send_message

        send_message(token, chat_id, text)
    except Exception as exc:
        log.warning("notify fail: %s", exc)


def _run_mlbb(env: dict[str, str]) -> dict:
    """One MLBB same-VOD montage with anti-run + cross-montage dedup."""
    cmd = [
        sys.executable,
        "-u",
        str(SCRIPTS / "mlbb_same_vod_montage_mission.py"),
        "--count",
        "1",
        "--env",
        str(ENV_PATH),
    ]
    child = {
        **env,
        "POST_QUOTA_MONTAGE_PASS": "1",
        "DAILY_GAME_CYCLE_ENABLED": "0",
        "MLBB_VOD_IGNORE_DAILY_QUOTA": "1",
        "MLBB_PRESEND_REJECT_RUN": "1",
        "MLBB_MONTAGE_COMBAT_GATE": "1",
        "MONTAGE_PREFER_FRESH_VOD": "1",
        "MONTAGE_ALLOW_VOD_REUSE": "0",
    }
    t0 = time.monotonic()
    proc = subprocess.run(cmd, env=child, check=False, capture_output=True, text=True)
    log.info(
        "mlbb montage rc=%s elapsed=%.0fs stdout=%s",
        proc.returncode,
        time.monotonic() - t0,
        (proc.stdout or "")[-500:],
    )
    if proc.stderr:
        log.info("mlbb montage stderr=%s", proc.stderr[-800:])
    ok = proc.returncode == 0
    return {"game": "mlbb", "ok": ok, "rc": proc.returncode}


def _run_shooter(game: str, env: dict[str, str]) -> dict:
    """One montage from shooter feed (PUBG/Standoff/Genshin/WoT)."""
    child = {
        **env,
        "VOD_SEGMENT_GAME": game,
        "POST_QUOTA_MONTAGE_PASS": "1",
        "VOD_IGNORE_DAILY_QUOTA": "1",
        f"{game.upper()}_VOD_IGNORE_DAILY_QUOTA": "1",
        # Bypass reliable-mode force that turns montage OFF.
        "SHOOTER_VOD_RELIABLE": "0",
        "SHOOTER_VOD_MONTAGE": "1",
        f"{game.upper()}_VOD_MONTAGE": "1",
        "SHOOTER_VOD_SEND_ONE": "1",
        "SHOOTER_VOD_MONTAGE_MIN_CLIPS": "2",
        "SHOOTER_VOD_MONTAGE_MAX_CLIPS": "4",
        "MONTAGE_PREFER_FRESH_VOD": "1",
        "MONTAGE_ALLOW_VOD_REUSE": "0",
        # Keep cycle env readable but don't gate sends.
        "DAILY_GAME_CYCLE_ENABLED": "0",
    }
    cmd = [sys.executable, "-u", str(SCRIPTS / "shooter_vod_segment_feed.py"), game]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, env=child, check=False, capture_output=True, text=True)
    log.info(
        "%s montage rc=%s elapsed=%.0fs stdout=%s",
        game,
        proc.returncode,
        time.monotonic() - t0,
        (proc.stdout or "")[-500:],
    )
    if proc.stderr:
        log.info("%s montage stderr=%s", game, proc.stderr[-800:])
    return {"game": game, "ok": proc.returncode == 0, "rc": proc.returncode}


def run_once(*, token: str = "", chat_id: str = "", max_games: int = 1) -> dict:
    """
    Produce up to `max_games` pending post-quota montages (default 1 per runner tick
    so we don't monopolize the VPS after quotas close).
    """
    if not post_quota_enabled():
        return {"skipped": True, "reason": "disabled"}
    day = _today_key()
    pending = games_pending_today()
    if not pending:
        return {"skipped": True, "reason": "all_done", "day": day}

    reports: list[dict] = []
    for game in pending[: max(1, max_games)]:
        log.info("post-quota montage start game=%s day=%s", game, day)
        if game == "mlbb":
            rep = _run_mlbb(dict(os.environ))
        else:
            rep = _run_shooter(game, dict(os.environ))
        reports.append(rep)
        # Mission / feed already mark dedup on success; if they forgot, at least
        # mark the day slot so we don't spin forever on a broken game.
        if rep.get("ok"):
            if not day_montage_done(game, day):
                mark_montage_sent(
                    game,
                    day=day,
                    vod_id="unknown",
                    peaks=[],
                    montage_id="post_quota_ok",
                )
            _notify(
                token,
                chat_id,
                f"🎬 Post-quota склейка: {game.upper()} готова "
                f"({status_summary().get('day')})",
            )
        else:
            log.warning("post-quota montage failed game=%s rc=%s", game, rep.get("rc"))
            # Don't burn the day slot on failure — retry next tick / next VOD.
    return {"day": day, "pending_before": pending, "reports": reports}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from youtube_download import load_env

    env = {**os.environ, **load_env(ENV_PATH)}
    os.environ.update(env)
    token = env.get("TG_BOT_TOKEN", "").strip()
    chat = (env.get("TG_CHAT_ID") or "").strip()
    max_games = int(os.environ.get("POST_QUOTA_MONTAGE_PER_TICK", "1"))
    out = run_once(token=token, chat_id=chat, max_games=max_games)
    log.info("post_quota result=%s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
