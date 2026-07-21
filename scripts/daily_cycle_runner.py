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


def _cleanup_exhausted_inbox(env: dict[str, str]) -> None:
    """Delete fully spent inbox VODs so disk does not fill with dead files."""
    if env.get("VOD_INBOX_DELETE_EXHAUSTED", "1") != "1":
        return
    try:
        from vod_inbox_cleanup import cleanup_all_games

        rows = cleanup_all_games(dry_run=False)
        deleted = sum(int(r.get("deleted") or 0) for r in rows)
        freed = sum(int(r.get("freed_bytes") or 0) for r in rows)
        if deleted:
            log.info(
                "inbox exhausted cleanup deleted=%s freed_gb=%.2f",
                deleted,
                freed / (1024**3),
            )
    except Exception:
        log.exception("inbox exhausted cleanup failed")


def _prefetch_search_pools(env: dict[str, str], active: str | None) -> None:
    """Warm per-game YouTube candidate pools so the active feed rarely blocks on search."""
    if env.get("VOD_SEARCH_POOL_ENABLED", "1") != "1":
        return
    if env.get("VOD_SEARCH_POOL_PREFETCH", "1") != "1":
        return
    try:
        import concurrent.futures
        from daily_game_cycle import GAME_ORDER, quota_remaining
        from vod_search_pool import prefetch_pools

        games = [g for g in GAME_ORDER if quota_remaining(g) > 0]
        if active and active not in games:
            games.insert(0, active)
        if not games:
            return
        timeout_sec = float(env.get("VOD_SEARCH_POOL_PREFETCH_TIMEOUT_SEC", "90"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(prefetch_pools, games, env, False)
            try:
                results = fut.result(timeout=timeout_sec)
            except concurrent.futures.TimeoutError:
                log.warning(
                    "search pools prefetch timed out after %.0fs — continue to feed",
                    timeout_sec,
                )
                return
        summary = ", ".join(
            f"{r.get('game')}={'+' if r.get('refreshed') else '='}{r.get('depth', '?')}" for r in results
        )
        log.info("search pools prefetch: %s", summary or "none")
    except Exception:
        log.exception("search pool prefetch failed")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = _load_runtime_env()
    if not enabled():
        _cleanup_exhausted_inbox(env)
        _prefetch_search_pools(env, "mlbb")
        proc = subprocess.run([sys.executable, "-u", str(SCRIPTS / "mlbb_vod_segment_feed.py")], check=False)
        return proc.returncode

    reset_if_new_day()
    game = active_game()
    token = env.get("TG_BOT_TOKEN", "").strip()
    chat_id = env.get("TG_CHAT_ID", "").strip()

    if game is None:
        log.info("all daily quotas done — idle")
        if token and chat_id:
            from mlbb_vod_segment_feed import send_message

            send_message(token, chat_id, "✅ Дневные квоты MLBB/PUBG/Standoff/Genshin/WoT выполнены. Жду 00:00.")
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

    # Free disk from fully mined VODs, then warm search pools for remaining quotas.
    _cleanup_exhausted_inbox(env)
    _prefetch_search_pools(env, game)

    if game == "mlbb":
        script = SCRIPTS / "mlbb_vod_segment_feed.py"
    else:
        script = SCRIPTS / "shooter_vod_segment_feed.py"
        env["VOD_SEGMENT_GAME"] = game

    proc = subprocess.run(
        [sys.executable, "-u", str(script)] + ([] if game == "mlbb" else [game]),
        env=env,
        check=False,
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
