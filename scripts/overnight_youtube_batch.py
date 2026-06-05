#!/usr/bin/env python3
"""
Daily batch: long YouTube VOD → montages → Telegram.
Resilient: retries, per-game checkpoint, resume, continue on errors.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ENV_FILE = Path("/root/.video_bot.env")
CONFIG = Path(os.environ.get("OVERNIGHT_GAMES_CONFIG", "/root/content_bot_ml/config/overnight_games.yaml"))
WORK_ROOT = Path("/root/data/mlbb/overnight_msk")
STATE_FILE = WORK_ROOT / "state.json"
LOG_FILE = WORK_ROOT / "batch.log"
PROCESSOR = Path("/usr/local/bin/smart_video_editor.py")
OUTPUT_DIR = Path("/root/videos")
MSK = ZoneInfo("Europe/Moscow")
DOWNLOAD_RETRIES = int(os.environ.get("OVERNIGHT_DOWNLOAD_RETRIES", "3"))
MONTAGE_RETRIES = int(os.environ.get("OVERNIGHT_MONTAGE_RETRIES", "2"))


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def clean_proxy_env(env: dict[str, str]) -> dict[str, str]:
    out = dict(env)
    for key in list(out):
        if "proxy" in key.lower():
            out.pop(key, None)
    return out


def deadline_utc(hour_msk: int = 8) -> datetime:
    now_msk = datetime.now(MSK)
    stop = now_msk.replace(hour=hour_msk, minute=0, second=0, microsecond=0)
    if stop <= now_msk:
        stop += timedelta(days=1)
    return stop.astimezone(timezone.utc)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"used_by_game": {}, "game_status": {}, "runs": []}


def save_state(state: dict) -> None:
    import os

    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    if STATE_FILE.exists():
        try:
            STATE_FILE.replace(STATE_FILE.with_suffix(".json.bak"))
        except OSError:
            pass
    tmp = STATE_FILE.with_suffix(f".json.tmp.{os.getpid()}")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def send_text(env: dict[str, str], chat_id: str, text: str) -> None:
    token = env.get("TG_BOT_TOKEN", "")
    if not token or not chat_id:
        return
    run_env = clean_proxy_env(os.environ.copy())
    run_env["TG_BOT_TOKEN"] = token
    subprocess.run(
        [
            "curl",
            "--noproxy",
            "*",
            "-sS",
            "-m",
            "60",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"text={text[:3900]}",
            f"https://api.telegram.org/bot{token}/sendMessage",
        ],
        check=False,
        capture_output=True,
        env=run_env,
    )


def game_done(state: dict, game: dict) -> bool:
    gid = game["id"]
    row = (state.get("game_status") or {}).get(gid) or {}
    need = int(game.get("montages", 1))
    return int(row.get("montages_ok") or 0) >= need and row.get("status") == "ok"


def discover_pick(game: dict, env: dict[str, str], used: set[str]) -> tuple[dict | None, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from nightly_youtube_montage import candidates_from_urls, discover_candidates, pick_candidate

    max_sec = float(game["max_duration_sec"])
    min_sec = float(game["min_duration_sec"])
    fb_min = float(game.get("fallback_min_duration_sec", min_sec))

    for label, floor in (("search_2h", min_sec), ("search_1.5h", fb_min)):
        if floor > max_sec:
            continue
        candidates = discover_candidates(
            env,
            queries=game["queries"],
            min_sec=floor,
            max_sec=max_sec,
            search_limit=int(game.get("search_limit", 12)),
            game_prefs=game,
        )
        pick = pick_candidate(candidates, used, game_prefs=game)
        if pick:
            return pick, label

    for url in game.get("fallback_urls") or []:
        fb = candidates_from_urls(
            [url],
            env,
            min_sec=fb_min,
            max_sec=max_sec,
            used_ids=used,
        )
        pick = pick_candidate(fb, used, game_prefs=game)
        if pick:
            return pick, "fallback_url"
    return None, "no_candidate"


def existing_inbox_video(pick: dict) -> Path | None:
    """Reuse VOD already on disk (saves hours after a failed overnight run)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from nightly_youtube_montage import INBOX

    vid = str(pick.get("id") or "").strip()
    if not vid:
        return None
    path = INBOX / f"yt_{vid}.mp4"
    if path.is_file() and path.stat().st_size > 50_000_000:
        logging.info("reuse inbox %s (%.1f MB)", path.name, path.stat().st_size / 1e6)
        return path
    return None


def download_with_retries(pick: dict, env: dict[str, str]) -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from nightly_youtube_montage import download_video

    cached = existing_inbox_video(pick)
    if cached:
        return cached

    last_exc: Exception | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            return download_video(pick, env)
        except Exception as exc:
            last_exc = exc
            logging.warning("download attempt %s/%s failed: %s", attempt, DOWNLOAD_RETRIES, exc)
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(20 * attempt)
    raise last_exc or RuntimeError("download failed")


def run_montage(
    source: Path,
    env: dict[str, str],
    chat_id: str,
    game: dict,
    variant: int,
    *,
    skip_mark_used: bool,
    relaxed: bool = False,
) -> tuple[int, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from montage_env import profile_montage_env

    profile = game["profile"]
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".queue.txt") as tmp:
        tmp.write(f"{source.resolve()}|{game['queue_label']}|{chat_id}\n")
        queue_path = tmp.name

    run_env = clean_proxy_env(os.environ.copy())
    for key, value in env.items():
        run_env.setdefault(key, value)
    run_env.update(profile_montage_env(profile))
    if relaxed and profile in ("mobile_legends", "mlbb"):
        run_env.update(
            {
                "SMART_MLBB_PEAK_PERCENTILE": "48",
                "SMART_MIN_MINIMAP_PRESENCE": "0.62",
                "SMART_MIN_CENTER_MOTION": "0.015",
                "MIN_HIGHLIGHTS": "4",
                "MAX_HIGHLIGHTS": "5",
            }
        )
    elif relaxed and profile == "pubg":
        run_env.update(
            {
                "SMART_PUBG_PEAK_PERCENTILE": "24",
                "SMART_PUBG_COMBAT_MIN": "0.06",
                "SMART_PUBG_SUSTAIN_PERCENTILE": "22",
                "MIN_HIGHLIGHTS": "4",
                "MAX_HIGHLIGHTS": "5",
            }
        )
    elif relaxed:
        run_env.update(
            {
                "SMART_PUBG_PEAK_PERCENTILE": "48",
                "SMART_PUBG_COMBAT_MIN": "0.12",
                "SMART_PUBG_SUSTAIN_PERCENTILE": "40",
                "MIN_HIGHLIGHTS": "3",
                "MAX_HIGHLIGHTS": "4",
            }
        )
    gid = game["id"]
    run_env.update(
        {
            "QUEUE_FILE": queue_path,
            "MAX_SOURCES": "1",
            "SINGLE_SOURCE_MODE": "1",
            "SEND_TELEGRAM": "1",
            "OVERNIGHT_BATCH": "1",
            "OVERNIGHT_FRESH_SEGMENTS": "1",
            "SEGMENT_HISTORY_FILE": str(WORK_ROOT / f"segment_history_{gid}.json"),
            "SMART_BLOCKING_LOCK": "1",
            "TG_CHAT_ID": chat_id,
            "TG_BOT_TOKEN": env.get("TG_BOT_TOKEN", ""),
            "OUTPUT_DIR": str(OUTPUT_DIR),
            "DEFAULT_GAME_PROFILE": profile,
            "QUEUE_GAME_PROFILE": profile,
            "SELECTION_VARIANT": str(variant),
            "SMART_SKIP_MARK_USED": "1" if skip_mark_used else "0",
            "SMART_MAKE_TIMEOUT_SEC": env.get("SMART_MAKE_TIMEOUT_SEC", "10800"),
            "SMART_MAKE_TIMEOUT_MAX_SEC": env.get("SMART_MAKE_TIMEOUT_MAX_SEC", "14400"),
            "MONTAGE_CAPTION": (
                f"🎬 {game['queue_label']} | ночной батч\n"
                f"Источник: {source.name[:40]}"
            ),
        }
    )
    timeout = int(float(run_env.get("SMART_MAKE_TIMEOUT_MAX_SEC", "14400")))
    try:
        completed = subprocess.run(
            [sys.executable, str(PROCESSOR)],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return completed.returncode, (completed.stderr or completed.stdout or "")[-1200:]
    finally:
        Path(queue_path).unlink(missing_ok=True)


def run_montage_with_retries(
    source: Path,
    env: dict[str, str],
    chat_id: str,
    game: dict,
    variant: int,
    *,
    skip_mark_used: bool,
) -> tuple[int, str]:
    last_code, last_tail = 1, ""
    for attempt in range(1, MONTAGE_RETRIES + 1):
        relaxed = attempt > 1
        code, tail = run_montage(
            source,
            env,
            chat_id,
            game,
            variant,
            skip_mark_used=skip_mark_used,
            relaxed=relaxed,
        )
        if code == 0:
            return code, tail
        last_code, last_tail = code, tail
        logging.warning("%s montage retry %s/%s", game["id"], attempt, MONTAGE_RETRIES)
        if attempt < MONTAGE_RETRIES:
            time.sleep(15)
    return last_code, last_tail


def process_game(
    game: dict,
    env: dict[str, str],
    chat_id: str,
    state: dict,
    stop_at: datetime,
) -> dict:
    gid = game["id"]
    used = set((state.get("used_by_game") or {}).get(gid, []))
    report: dict = {
        "game": gid,
        "profile": game["profile"],
        "montages_ok": 0,
        "skipped": None,
        "status": "pending",
    }

    if datetime.now(timezone.utc) >= stop_at:
        report["skipped"] = "deadline_before_start"
        report["status"] = "deadline"
        return report

    logging.info("=== game %s ===", gid)
    pick, src_kind = discover_pick(game, env, used)
    if not pick:
        report["skipped"] = "no_candidate"
        report["status"] = "no_candidate"
        logging.warning("no candidate for %s", gid)
        send_text(env, chat_id, f"⚠️ [{game['queue_label']}] не нашёл подходящий стрим на YouTube.")
        return report

    report["source_kind"] = src_kind
    send_text(
        env,
        chat_id,
        f"▶️ [{game['queue_label']}] качаю ~{int(pick['duration'] // 60)} мин ({src_kind})\n"
        f"{pick['title'][:160]}",
    )

    try:
        source = download_with_retries(pick, env)
    except Exception as exc:
        report["skipped"] = f"download:{exc}"
        report["status"] = "download_failed"
        logging.exception("download %s", gid)
        send_text(env, chat_id, f"⚠️ [{game['queue_label']}] не скачалось после {DOWNLOAD_RETRIES} попыток.")
        return report

    logging.info("%s downloaded %s", gid, source)
    montages = int(game.get("montages", 1))
    ok = 0
    for variant in range(montages):
        if datetime.now(timezone.utc) >= stop_at:
            report["skipped"] = "deadline_mid_game"
            break
        logging.info("%s montage %s/%s", gid, variant + 1, montages)
        skip_mark = variant < montages - 1
        code, tail = run_montage_with_retries(
            source,
            env,
            chat_id,
            game,
            variant,
            skip_mark_used=skip_mark,
        )
        if code == 0:
            ok += 1
        else:
            logging.error("%s variant %s failed: %s", gid, variant, tail[-400:])
            report["montage_error"] = tail[-300:]
        time.sleep(6)

    report["montages_ok"] = ok
    report["video_id"] = pick["id"]
    report["title"] = pick["title"][:120]
    report["url"] = pick["url"]
    report["status"] = "ok" if ok > 0 else "montage_failed"
    if ok > 0:
        used.add(pick["id"])
        state.setdefault("used_by_game", {})[gid] = list(used)[-80:]
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from publish_ready_montage import latest_in_output_dir, register  # noqa: WPS433

            montage = latest_in_output_dir()
            if montage:
                register(montage, source_url=pick.get("url", ""), title=pick.get("title", ""))
        except Exception as exc:
            logging.warning("%s manifest: %s", gid, exc)
        send_text(env, chat_id, f"✅ [{game['queue_label']}] готово: {ok} нарезка(ок).")
    else:
        send_text(
            env,
            chat_id,
            f"⚠️ [{game['queue_label']}] скачано, нарезка не собралась "
            f"(мало боевых пиков). Перезапущу с мягче отбором.",
        )

    state.setdefault("game_status", {})[gid] = {
        **report,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_state(state)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument(
        "--deadline-hour-msk",
        type=int,
        default=int(os.environ.get("OVERNIGHT_DEADLINE_HOUR_MSK", "8")),
    )
    parser.add_argument(
        "--stop-in-hours",
        type=float,
        default=float(os.environ.get("OVERNIGHT_STOP_IN_HOURS", "0") or "0"),
    )
    parser.add_argument("--only-games", type=str, default="", help="comma list: mlbb,pubg,...")
    parser.add_argument("--resume", action="store_true", help="skip games already ok in state")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    if not args.config.exists():
        logging.error("config missing: %s", args.config)
        return 1

    games_cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    games = games_cfg.get("games") or []
    only = {g.strip() for g in args.only_games.split(",") if g.strip()}
    if only:
        games = [g for g in games if g["id"] in only]

    env = load_env()
    chat_id = env.get("TG_CHAT_ID", "")
    if not env.get("TG_BOT_TOKEN") or not chat_id:
        logging.error("TG_BOT_TOKEN / TG_CHAT_ID missing")
        return 1

    if args.stop_in_hours > 0:
        stop_at = datetime.now(timezone.utc) + timedelta(hours=args.stop_in_hours)
        logging.info("stop in %.1f h (catch-up)", args.stop_in_hours)
    else:
        stop_at = deadline_utc(args.deadline_hour_msk)
        logging.info("deadline UTC %s (%s:00 MSK)", stop_at.isoformat(), args.deadline_hour_msk)

    if args.dry_run:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        state = load_state()
        for game in games:
            used = set((state.get("used_by_game") or {}).get(game["id"], []))
            pick, kind = discover_pick(game, env, used)
            print(game["id"], kind, pick["title"][:50] if pick else "-")
        return 0

    state = load_state()
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    send_text(
        env,
        chat_id,
        f"▶️ Батч старт {started} МСК.\n"
        f"Игры: {', '.join(g['id'] for g in games)}. Ошибка по одной игре — идём дальше.",
    )

    results: list[dict] = []
    for game in games:
        if args.resume and game_done(state, game):
            logging.info("skip %s (resume ok)", game["id"])
            results.append({"game": game["id"], "skipped": "resume_ok", "montages_ok": 1})
            continue
        if datetime.now(timezone.utc) >= stop_at:
            results.append({"game": game["id"], "skipped": "deadline"})
            continue
        try:
            results.append(process_game(game, env, chat_id, state, stop_at))
        except Exception as exc:
            logging.exception("fatal %s", game["id"])
            results.append({"game": game["id"], "skipped": f"error:{exc}", "status": "error"})
            send_text(env, chat_id, f"⚠️ [{game.get('queue_label', game['id'])}] сбой: {exc}")
            save_state(state)

    save_state(state)
    WORK_ROOT.joinpath("last_report.json").write_text(
        json.dumps({"started": started, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [f"🌅 Батч завершён ({started} МСК):"]
    for row in results:
        gid = row.get("game", "?")
        if row.get("skipped"):
            lines.append(f"• {gid}: {row['skipped']}")
        else:
            lines.append(f"• {gid}: {row.get('montages_ok', 0)} нарезок ({row.get('status', '?')})")
    send_text(env, chat_id, "\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
