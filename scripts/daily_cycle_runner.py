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

from daily_game_cycle import (
    active_game,
    enabled,
    force_skip_game,
    is_game_stalled,
    load_state,
    note_feed_iteration,
    reset_if_new_day,
    save_state,
    send_count,
    status_summary,
)
from youtube_download import load_env

log = logging.getLogger("daily_cycle_runner")
ENV_PATH = Path("/root/.video_bot.env")
SCRIPTS = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml")) / "scripts"


def _idle_stalled_sleep_sec() -> int:
    """When all remaining games are stalled, do not spin every 8s and spam TG.

    If local media still exists, sleep briefly so self-heal can resume — never 15min
    blackouts with rem>0 (owner SLA: ≥1 video/hour).
    """
    try:
        from daily_game_cycle import GAME_ORDER, _game_inbox_ready, quota_remaining

        has_media = any(
            quota_remaining(g) > 0 and _game_inbox_ready(g) for g in GAME_ORDER
        )
        if has_media:
            return max(120, int(os.environ.get("DAILY_CYCLE_STALLED_IDLE_WITH_MEDIA_SEC", "600")))
        return max(300, int(os.environ.get("DAILY_CYCLE_STALLED_IDLE_SEC", "900")))
    except Exception:
        return 120


def _notify_once(token: str, chat_id: str, key: str, text: str) -> bool:
    """Send Telegram notify at most once per day for this key (persisted in cycle state)."""
    if not token or not chat_id or not key:
        return False
    state = load_state()
    notified = state.setdefault("notified", {})
    if notified.get(key):
        return False
    try:
        from mlbb_vod_segment_feed import send_message

        send_message(token, chat_id, text)
    except Exception as exc:  # noqa: BLE001
        log.warning("notify failed key=%s: %s", key, exc)
        return False
    notified[key] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    return True


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


def _park_inbox_head_after_timeout(game: str) -> str | None:
    """Move the hung inbox VOD aside so the next loop does not re-enter the same hang."""
    roots = {
        "mlbb": Path("/root/data/mlbb/youtube_nightly/inbox"),
        "pubg": Path("/root/data/pubg/youtube_nightly/inbox"),
        "standoff": Path("/root/data/standoff/youtube_nightly/inbox"),
        "genshin": Path("/root/data/genshin/youtube_nightly/inbox"),
        "wot": Path("/root/data/wot/youtube_nightly/inbox"),
    }
    inbox = roots.get(game)
    if inbox is None or not inbox.is_dir():
        return None
    vods = sorted(inbox.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    if not vods:
        return None
    # Prefer the VOD currently named in recent logs; else oldest inbox file.
    target = vods[0]
    try:
        log_path = Path("/root/data/mlbb/mlbb_vod_segment_feed.log")
        if log_path.exists():
            tail = log_path.read_text(errors="replace")[-8000:]
            for name in reversed([p.name for p in vods]):
                if name in tail and ("parallel score" in tail or "TIMEOUT" in tail or "highlight" in tail):
                    hit = inbox / name
                    if hit.exists():
                        target = hit
                        break
    except OSError:
        pass
    park = inbox.parent / "park_timeout"
    park.mkdir(parents=True, exist_ok=True)
    dest = park / target.name
    if dest.exists():
        dest = park / f"{target.stem}_{int(time.time())}{target.suffix}"
    try:
        target.rename(dest)
        return dest.name
    except OSError as exc:
        log.warning("park timeout vod failed %s: %s", target, exc)
        return None


def _run_timeout_sec() -> int:
    try:
        return max(120, int(os.environ.get("DAILY_CYCLE_RUN_TIMEOUT_SEC", "1800")))
    except ValueError:
        return 1800


def _ensure_disk_headroom() -> None:
    """ENOSPC silently killed the feed for 6h — cleanup before each iteration."""
    import shutil

    try:
        free_gb = shutil.disk_usage("/").free / (1024**3)
    except OSError as exc:
        log.warning("disk usage check failed: %s", exc)
        return
    min_free = float(os.environ.get("VPS_DISK_MIN_FREE_GB", "8"))
    if free_gb >= min_free:
        return
    log.error("disk low free=%.1fGB (need>=%.1f) — running cleanup", free_gb, min_free)
    script = SCRIPTS / "vps_disk_cleanup.sh"
    if script.is_file():
        try:
            subprocess.run(["bash", str(script)], check=False, timeout=240)
        except Exception as exc:
            log.warning("disk cleanup failed: %s", exc)
    # Emergency parks wipe if still critical.
    try:
        free2 = shutil.disk_usage("/").free / (1024**3)
    except OSError:
        return
    if free2 >= min_free * 0.5:
        log.warning("disk after cleanup free=%.1fGB", free2)
        return
    for g in ("mlbb", "pubg", "standoff", "genshin", "wot"):
        base = Path(f"/root/data/{g}/youtube_nightly")
        for sub in ("hold_quota", "hold_barren", "park_dead", "park_timeout", "exhausted"):
            p = base / sub
            if p.is_dir():
                subprocess.run(["rm", "-rf", str(p)], check=False)
                p.mkdir(parents=True, exist_ok=True)
    try:
        log.warning("disk emergency free=%.1fGB", shutil.disk_usage("/").free / (1024**3))
    except OSError:
        pass


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = _load_runtime_env()
    _ensure_disk_headroom()
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
            # Last chance: clear stall when inbox still has VODs (avoid 8h idle).
            try:
                from daily_game_cycle import unstall_games_with_inbox

                cleared = unstall_games_with_inbox()
                if cleared:
                    log.warning("unstalled games with inbox ready: %s", cleared)
                    game = active_game()
            except Exception as exc:
                log.warning("unstall check failed: %s", exc)
        if game is None:
            rem = status_summary()["remaining"]
            if any(int(v) > 0 for v in rem.values()):
                log.warning("all remaining games stalled — idle rem=%s", rem)
                day = status_summary().get("day") or "today"
                _notify_once(
                    token,
                    chat_id,
                    f"all_stalled:{day}",
                    f"⚠️ Цикл: оставшиеся игры залипли (stall skip). "
                    f"Остаток квот: {rem}. Жду правки inbox/discovery или 00:00. "
                    f"(это сообщение один раз, не спам)",
                )
                # Stop 8s busy-loop: sleep here so wrapper does not re-notify/re-log every tick.
                time.sleep(_idle_stalled_sleep_sec())
            else:
                log.info("all daily quotas done — idle")
                _notify_once(
                    token,
                    chat_id,
                    "quotas_done",
                    "✅ Дневные квоты MLBB/PUBG/Standoff/Genshin/WoT выполнены. Жду 00:00.",
                )
                time.sleep(_idle_stalled_sleep_sec())
            return 0

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
    thrash = False
    try:
        proc = subprocess.run(argv, env=env, check=False, timeout=timeout)
        rc = int(proc.returncode or 0)
    except subprocess.TimeoutExpired:
        timed_out = True
        thrash = True
        log.error("TIMEOUT game=%s after %ss — kill hang", game, timeout)
        subprocess.run(["pkill", "-f", f"shooter_vod_segment_feed.py {game}"], check=False)
        subprocess.run(["pkill", "-f", "mlbb_vod_segment_feed.py"], check=False)
        subprocess.run(["pkill", "-f", "yt-dlp"], check=False)
        subprocess.run(["pkill", "-f", "ffmpeg"], check=False)
        for lock in (
            f"/tmp/{game}_vod_segment_feed.lock",
            "/tmp/mlbb_vod_segment_feed.lock",
            "/tmp/mlbb_vod_ytdlp.lock",
        ):
            try:
                Path(lock).unlink(missing_ok=True)
            except OSError:
                pass
        # One Telegram line per game per day — never spam every 10 minutes.
        day = time.strftime("%Y-%m-%d")
        _notify_once(
            token,
            chat_id,
            f"timeout:{game}:{day}",
            f"⏱️ Антизависание: {game.upper()} timeout {timeout}s — процесс убит "
            f"(это сообщение 1 раз в день на игру, дальше только в лог).",
        )
        parked = _park_inbox_head_after_timeout(game)
        if parked:
            log.warning("timeout parked hung vod game=%s file=%s", game, parked)
        rc = 124
        # Heal locks/procs but do NOT clear stall for this game (that caused the spam loop).
        try:
            from cycle_self_heal import heal_once

            heal_once(exclude_unstall={game})
        except TypeError:
            try:
                from cycle_self_heal import heal_once

                heal_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("post-timeout self-heal failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("post-timeout self-heal failed: %s", exc)

    after = send_count(game)
    delta = max(0, after - before)
    # Thrash = true idle loops only (inbox_dead / discovery pause), NOT every
    # normal sent=0 vods=0 discovery miss — that force-skipped shooters in ~5min.
    try:
        log_path = Path("/root/data/mlbb/mlbb_vod_segment_feed.log")
        if log_path.exists():
            tail = log_path.read_text(errors="replace")[-6000:]
            for line in reversed(tail.splitlines()[-40:]):
                if f"game={game}" not in line and f" {game}" not in line:
                    # Prefer same-game lines; still allow inbox_dead without game tag.
                    if "inbox_dead=1" not in line:
                        continue
                if "inbox_dead=1" in line:
                    thrash = True
                    break
                if "discovery_miss=1" in line and f"game={game}" in line:
                    thrash = True
                    break
                if "discovery paused" in line and "game=" + game in line:
                    thrash = True
                    break
                if "no scannable inbox + discovery paused" in line:
                    thrash = True
                    break
                if "banner miss streak=" in line and game == "mlbb":
                    thrash = True
                    break
    except OSError:
        pass

    entry = note_feed_iteration(game, delta, thrash=thrash, timed_out=timed_out)
    log.info(
        "done game=%s sent_delta=%s zero_runs=%s thrash_runs=%s timeout_runs=%s stalled=%s timed_out=%s",
        game,
        delta,
        entry.get("zero_runs"),
        entry.get("thrash_runs"),
        entry.get("timeout_runs"),
        is_game_stalled(game),
        timed_out,
    )
    timeout_limit = max(2, int(os.environ.get("DAILY_CYCLE_TIMEOUT_SKIP_AFTER", "4")))
    if timed_out and int(entry.get("timeout_runs") or 0) >= timeout_limit:
        # Never burn the day's remaining quota while usable VODs still sit in inbox.
        # Hung VODs are already parked; keep scanning the rest.
        has_local = False
        try:
            from daily_game_cycle import game_has_ready_media

            has_local = bool(game_has_ready_media(game))
        except Exception:
            has_local = False
        if has_local:
            log.warning(
                "timeout streak game=%s n=%s but local media remains — continue (no force_skip)",
                game,
                entry.get("timeout_runs"),
            )
            # Soft-decay so one bad file cannot escalate forever.
            try:
                state = load_state()
                stall = state.setdefault("stall", {})
                soft = stall.setdefault(game, {})
                soft["timeout_runs"] = max(0, int(soft.get("timeout_runs") or 0) - 1)
                soft["force_skip"] = False
                stall[game] = soft
                save_state(state)
            except Exception as exc:  # noqa: BLE001
                log.warning("timeout soft-decay failed: %s", exc)
            return rc
        force_skip_game(
            game,
            reason=f"timeout_x{entry.get('timeout_runs')} hung_highlight",
        )
        nxt = active_game()
        _notify_once(
            token,
            chat_id,
            f"timeout_skip:{game}:{time.strftime('%Y-%m-%d')}",
            f"⏭ {game.upper()}: {entry.get('timeout_runs')} timeout подряд — skip на сегодня. "
            f"Следующая: {nxt or 'нет'}",
        )
        return rc
    if delta == 0 and is_game_stalled(game):
        # Never burn remaining quota while usable local VODs still exist.
        has_local = False
        try:
            from daily_game_cycle import game_has_ready_media

            has_local = bool(game_has_ready_media(game))
        except Exception:
            has_local = False
        if has_local and not timed_out:
            log.warning(
                "stall hold game=%s — local usable media present; self-heal instead of skip",
                game,
            )
            try:
                from cycle_self_heal import heal_once

                heal_once(exclude_unstall={game})
            except TypeError:
                try:
                    from cycle_self_heal import heal_once

                    heal_once()
                except Exception as exc:  # noqa: BLE001
                    log.warning("stall-hold heal failed: %s", exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("stall-hold heal failed: %s", exc)
            time.sleep(15.0)
            return rc
        force_skip_game(game, reason=f"stall thrash={entry.get('thrash_runs')} zero={entry.get('zero_runs')} timeout={timed_out}")
        nxt = active_game()
        _notify_once(
            token,
            chat_id,
            f"stall_skip:{game}",
            f"⏭ Stall-skip {game.upper()}: нет прогресса. "
            f"Следующая: {nxt or 'нет (все done/stall)'}",
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
