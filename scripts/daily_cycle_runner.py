#!/usr/bin/env python3
"""Dispatch one VOD feed iteration for the active daily game (MLBB → PUBG → Standoff → Genshin → WoT)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from daily_game_cycle import active_game, enabled, reset_if_new_day, status_summary
from youtube_download import load_env

log = logging.getLogger("daily_cycle_runner")
ENV_PATH = Path("/root/.video_bot.env")
SCRIPTS = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml")) / "scripts"


def _notify_switch(token: str, chat_id: str, game: str) -> None:
    from mlbb_vod_segment_feed import send_message

    labels = {
        "mlbb": "MLBB",
        "pubg": "PUBG",
        "standoff": "Standoff 2",
        "genshin": "Genshin",
        "wot": "WoT",
    }
    send_message(
        token,
        chat_id,
        f"🔄 Дневной цикл: активна игра {labels.get(game, game)}\n"
        f"Квоты: {status_summary()['remaining']}",
    )


def _load_runtime_env() -> dict[str, str]:
    env = {**os.environ, **load_env(ENV_PATH)}
    os.environ.update(env)
    return env


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = _load_runtime_env()
    if not enabled():
        proc = subprocess.run([sys.executable, "-u", str(SCRIPTS / "mlbb_vod_segment_feed.py")], check=False)
        return proc.returncode

    reset_if_new_day()
    game = active_game()
    token = env.get("TG_BOT_TOKEN", "").strip()
    chat_id = env.get("TG_CHAT_ID", "").strip()

    if game is None:
        log.info("all daily quotas done — idle / post-quota montages")
        from daily_game_cycle import mark_notified, was_notified

        notify_key = f"quotas_done_{status_summary()['day']}"
        if token and chat_id and not was_notified(notify_key):
            from mlbb_vod_segment_feed import send_message

            send_message(
                token,
                chat_id,
                "✅ Дневные квоты MLBB/PUBG/Standoff/Genshin/WoT выполнены.\n"
                "Дальше: по 1 склейке на игру (без повторов пиков/VOD).",
            )
            mark_notified(notify_key)
        # After quotas: 1 montage per game (spread across runner ticks).
        try:
            from post_quota_montages import post_quota_enabled, run_once

            if post_quota_enabled():
                result = run_once(token=token, chat_id=chat_id, max_games=1)
                log.info("post_quota_montages: %s", result)
        except Exception:
            log.exception("post_quota_montages failed")
        return 0

    notify_key = f"active_{game}_{status_summary()['day']}"
    from daily_game_cycle import load_state, save_state

    state = load_state()
    if state.get("notified", {}).get("active_game") != game:
        if token and chat_id:
            _notify_switch(token, chat_id, game)
        notified = state.setdefault("notified", {})
        notified["active_game"] = game
        save_state(state)

    if game == "mlbb":
        script = SCRIPTS / "mlbb_vod_segment_feed.py"
    else:
        script = SCRIPTS / "shooter_vod_segment_feed.py"
        env["VOD_SEGMENT_GAME"] = game

    # Future switch: when quotas are stable, ship only montages (not singles).
    try:
        from post_quota_montages import montage_only_mode

        if montage_only_mode():
            env["MLBB_VOD_MONTAGE"] = "1"
            env["MLBB_SKIP_MONTAGE"] = "0"
            env["SHOOTER_VOD_MONTAGE"] = "1"
            env[f"{game.upper()}_VOD_MONTAGE"] = "1"
            env["SHOOTER_VOD_RELIABLE"] = "0"  # reliable mode forces montage off
            env["MLBB_VOD_RELIABLE"] = "0"
            env["MLBB_PRESEND_REJECT_RUN"] = "1"
            log.info("MONTAGE_ONLY_MODE active for game=%s", game)
    except Exception:
        pass

    proc = subprocess.run(
        [sys.executable, "-u", str(script)] + ([] if game == "mlbb" else [game]),
        env=env,
        check=False,
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
