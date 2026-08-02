#!/usr/bin/env python3
"""Dispatch one VOD feed iteration for the active daily game (MLBB → PUBG → Standoff → Genshin → WoT)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
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


# Launcher (/usr/local/bin/mlbb_vod_segment_feed.sh) exports quality/runtime
# knobs after sourcing .video_bot.env. Do not let the env file win on reload —
# that reset MLBB_VOD_AUTO_DOWNLOAD=0 and starved the feed of fresh VODs.
_LAUNCHER_PRESERVE_KEYS = (
    "MLBB_VOD_AUTO_DOWNLOAD",
    "MLBB_VOD_AUTO_DOWNLOAD_ON_EMPTY",
    "MLBB_OCR_DOUBLE_REQUIRE_LIVE",
    "MLBB_BANNER_OWN_HUD_MIN_SIM",
    "MLBB_BANNER_DISCOVER_POS_LIVE_MIN_SIM",
    "MLBB_BANNER_NEG_POS_MARGIN",
    "MLBB_BANNER_NEG_NOT_KILL_MIN",
    "MLBB_BANNER_RAPID_OCR",
    "MLBB_BANNER_LIVE_OVERLAY_OCR",
    "MLBB_BANNER_DISCOVER_RAPID_PEAKS",
    "MLBB_FIGHT_FIRST_ABORT_ON_MISS",
    "MLBB_KILL_BANNER_DISCOVER_MAX_SEC",
    "MLBB_KILL_BANNER_DISCOVER_MAX_PROBES",
    "MLBB_DISCOVER_SHIP_ON_FIRST",
    "MLBB_VOD_PREFETCH",
    "MLBB_VOD_INBOX_MAX",
    "MLBB_VOD_RELIABLE",
    "MLBB_VOD_KEEP_BANNER_MISS",
    "MLBB_VOD_REUSE_PEAK_POOL",
    "VOD_POOL_TTL_SEC",
    "MLBB_PRESEND_OWN_KILL_SINGLE",
    "MLBB_PRESEND_BANNER_CONTEXT",
    "MLBB_KILL_BANNER_DISCOVER_MERGE_TIER",
    "MLBB_PRESEND_LIVE_OCR_BUDGET",
    "SHOOTER_VOD_MONTAGE",
    "SHOOTER_VOD_MONTAGE_ONLY",
    "SHOOTER_VOD_MONTAGE_MIN_CLIPS",
    "SHOOTER_VOD_MONTAGE_MAX_CLIPS",
    "PUBG_VOD_MONTAGE",
    "STANDOFF_VOD_MONTAGE",
    "WOT_VOD_MONTAGE",
    "DAILY_GAME_PUBG_QUOTA",
    "DAILY_GAME_STANDOFF_QUOTA",
    "DAILY_GAME_WOT_QUOTA",
)


def _scrub_mlbb_only_env_for_shooter(env: dict[str, str]) -> None:
    """Launcher exports MLBB CLIP-off; shooters must score with CLIP."""
    env["HIGHLIGHT_CLIP_DISABLED"] = "0"
    os.environ["HIGHLIGHT_CLIP_DISABLED"] = "0"
    # Do not inherit MLBB banner OCR hang knobs into shooter highlight.
    for key in (
        "MLBB_BANNER_SKIP_CLIP_SCORE",
        "MLBB_STAGE1_SKIP_CLIP_RANK",
        "MLBB_STAGE1_SKIP_INTELLICLIP",
    ):
        env.pop(key, None)
        os.environ.pop(key, None)


def _load_runtime_env() -> dict[str, str]:
    preserved = {k: os.environ[k] for k in _LAUNCHER_PRESERVE_KEYS if k in os.environ}
    env = {**os.environ, **load_env(ENV_PATH), **preserved}
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
                # When locked / all done, avoid tight loop burn — feed.sh also sleeps.
                if result.get("reason") in {"locked", "all_done", "disabled"}:
                    time.sleep(float(os.environ.get("POST_QUOTA_IDLE_SEC", "20")))
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
        _scrub_mlbb_only_env_for_shooter(env)

    # Future: MONTAGE_ONLY_MODE for MLBB. PUBG/Standoff/WoT always ship 3×3 montages.
    try:
        from post_quota_montages import montage_only_mode

        if montage_only_mode():
            env["MLBB_VOD_MONTAGE"] = "1"
            env["MLBB_SKIP_MONTAGE"] = "0"
            env["SHOOTER_VOD_MONTAGE"] = "1"
            env[f"{game.upper()}_VOD_MONTAGE"] = "1"
            env["MLBB_PRESEND_REJECT_RUN"] = "1"
            log.info("MONTAGE_ONLY_MODE active for game=%s (reliable kept)", game)
    except Exception:
        pass

    if game in {"pubg", "standoff", "wot"}:
        env["SHOOTER_VOD_MONTAGE"] = "1"
        env["SHOOTER_VOD_MONTAGE_ONLY"] = "1"
        env["SHOOTER_VOD_MONTAGE_MIN_CLIPS"] = "3"
        env["SHOOTER_VOD_MONTAGE_MAX_CLIPS"] = "3"
        env[f"{game.upper()}_VOD_MONTAGE"] = "1"
        env[f"{game.upper()}_VOD_MONTAGE_ONLY"] = "1"
        log.info("shooter triple-montage quota game=%s (3 clips × 3 sends)", game)
    elif game == "genshin":
        # Boss fights stay single clips — global SHOOTER_VOD_MONTAGE must not glue them.
        env["GENSHIN_VOD_MONTAGE"] = "0"
        env["GENSHIN_VOD_MONTAGE_ONLY"] = "0"
        env["VOD_SEND_HQ_FILE"] = "1"

    proc = subprocess.run(
        [sys.executable, "-u", str(script)] + ([] if game == "mlbb" else [game]),
        env=env,
        check=False,
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
