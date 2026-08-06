#!/usr/bin/env python3
"""Dispatch one VOD feed iteration for the active daily game (MLBB → PUBG → Standoff → Genshin → WoT)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from daily_game_cycle import (
    active_game,
    enabled,
    force_skip_game,
    is_game_stalled,
    note_feed_iteration,
    reset_if_new_day,
    send_count,
    status_summary,
)
from youtube_download import load_env

log = logging.getLogger("daily_cycle_runner")
ENV_PATH = Path("/root/.video_bot.env")
SCRIPTS = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml")) / "scripts"


def _notify(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        return
    try:
        from mlbb_vod_segment_feed import send_message

        send_message(token, chat_id, text)
    except Exception as exc:  # noqa: BLE001
        log.warning("notify failed: %s", exc)


def _notify_switch(token: str, chat_id: str, game: str) -> None:
    labels = {
        "mlbb": "MLBB",
        "pubg": "PUBG",
        "standoff": "Standoff 2",
        "genshin": "Genshin",
        "wot": "WoT",
    }
    _notify(
        token,
        chat_id,
        f"🔄 Дневной цикл: активна игра {labels.get(game, game)}\n"
        f"Квоты: {status_summary()['remaining']}",
    )


def _load_runtime_env() -> dict[str, str]:
    env = {**os.environ, **load_env(ENV_PATH)}
    os.environ.update(env)
    return env


def _run_timeout_sec() -> int:
    try:
        return max(120, int(os.environ.get("DAILY_CYCLE_RUN_TIMEOUT_SEC", "1800")))
    except ValueError:
        return 1800


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = _load_runtime_env()
    if not enabled():
        proc = subprocess.run([sys.executable, "-u", str(SCRIPTS / "mlbb_vod_segment_feed.py")], check=False)
        return proc.returncode

    reset_if_new_day()
    token = env.get("TG_BOT_TOKEN", "").strip()
    chat_id = env.get("TG_CHAT_ID", "").strip()

    game = active_game()
    if game is None:
        rem = status_summary()["remaining"]
        if any(int(v) > 0 for v in rem.values()):
            log.warning("all remaining games stalled — idle rem=%s", rem)
            _notify(
                token,
                chat_id,
                f"⚠️ Цикл: оставшиеся игры залипли (stall skip). "
                f"Остаток квот: {rem}. Жду правки inbox/discovery или 00:00.",
            )
        else:
            log.info("all daily quotas done — idle")
            _notify(token, chat_id, "✅ Дневные квоты MLBB/PUBG/Standoff/Genshin/WoT выполнены. Жду 00:00.")
        return 0

    from daily_game_cycle import load_state, save_state

    state = load_state()
    if state.get("notified", {}).get("active_game") != game:
        _notify_switch(token, chat_id, game)
        notified = state.setdefault("notified", {})
        notified["active_game"] = game
        save_state(state)

    if game == "mlbb":
        script = SCRIPTS / "mlbb_vod_segment_feed.py"
        argv = [sys.executable, "-u", str(script)]
    else:
        script = SCRIPTS / "shooter_vod_segment_feed.py"
        env["VOD_SEGMENT_GAME"] = game
        argv = [sys.executable, "-u", str(script), game]

    before = send_count(game)
    timeout = _run_timeout_sec()
    log.info("run game=%s timeout=%ss sends_before=%s", game, timeout, before)
    timed_out = False
    try:
        proc = subprocess.run(argv, env=env, check=False, timeout=timeout)
        rc = int(proc.returncode or 0)
    except subprocess.TimeoutExpired:
        timed_out = True
        log.error("TIMEOUT game=%s after %ss — kill hang", game, timeout)
        subprocess.run(["pkill", "-f", f"shooter_vod_segment_feed.py {game}"], check=False)
        subprocess.run(["pkill", "-f", "yt-dlp"], check=False)
        _notify(token, chat_id, f"⏱️ Антизависание: {game.upper()} timeout {timeout}s — процесс убит.")
        rc = 124

    after = send_count(game)
    delta = max(0, after - before)
    entry = note_feed_iteration(game, delta)
    log.info(
        "done game=%s sent_delta=%s zero_runs=%s stalled=%s timed_out=%s",
        game,
        delta,
        entry.get("zero_runs"),
        is_game_stalled(game),
        timed_out,
    )
    if delta == 0 and is_game_stalled(game):
        force_skip_game(game, reason=f"zero_send_stall runs={entry.get('zero_runs')} timeout={timed_out}")
        nxt = active_game()
        _notify(
            token,
            chat_id,
            f"⏭ Stall-skip {game.upper()}: {entry.get('zero_runs')} нулевых прогонов. "
            f"Следующая: {nxt or 'нет (все done/stall)'}",
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
