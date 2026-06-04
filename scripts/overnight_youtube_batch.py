#!/usr/bin/env python3
"""
Midnight MSK → ~08:00 MSK: 5 games × long YouTube VOD × N montages.
Only automated Telegram video sends from this job (legacy crons disabled separately).
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
    """Stop starting new work after 08:00 MSK same calendar day as start."""
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
    return {"used_by_game": {}, "runs": []}


def save_state(state: dict) -> None:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def send_text(env: dict[str, str], chat_id: str, text: str) -> None:
    token = env.get("TG_BOT_TOKEN", "")
    if not token or not chat_id:
        return
    subprocess.run(
        [
            "curl",
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
        env=clean_proxy_env(os.environ),
    )


def run_montage(
    source: Path,
    env: dict[str, str],
    chat_id: str,
    game: dict,
    variant: int,
    *,
    skip_mark_used: bool,
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
    run_env.update(
        {
            "QUEUE_FILE": queue_path,
            "MAX_SOURCES": "1",
            "SINGLE_SOURCE_MODE": "1",
            "SEND_TELEGRAM": "1",
            "OVERNIGHT_BATCH": "1",
            "TG_CHAT_ID": chat_id,
            "TG_BOT_TOKEN": env.get("TG_BOT_TOKEN", ""),
            "OUTPUT_DIR": str(OUTPUT_DIR),
            "DEFAULT_GAME_PROFILE": profile,
            "QUEUE_GAME_PROFILE": profile,
            "STRICT_GAMEPLAY": "0",
            "SELECTION_VARIANT": str(variant),
            "SMART_SKIP_MARK_USED": "1" if skip_mark_used else "0",
            "SMART_MAKE_TIMEOUT_SEC": env.get("SMART_MAKE_TIMEOUT_SEC", "10800"),
            "SMART_MAKE_TIMEOUT_MAX_SEC": env.get("SMART_MAKE_TIMEOUT_MAX_SEC", "14400"),
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


def process_game(
    game: dict,
    env: dict[str, str],
    chat_id: str,
    state: dict,
    stop_at: datetime,
) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from nightly_youtube_montage import (
        candidates_from_urls,
        discover_candidates,
        download_video,
        pick_candidate,
    )

    gid = game["id"]
    used = set((state.get("used_by_game") or {}).get(gid, []))
    report = {"game": gid, "profile": game["profile"], "montages_ok": 0, "skipped": None}

    if datetime.now(timezone.utc) >= stop_at:
        report["skipped"] = "deadline_before_start"
        return report

    logging.info("=== game %s ===", gid)
    max_sec = float(game["max_duration_sec"])
    min_sec = float(game["min_duration_sec"])
    candidates = discover_candidates(
        env,
        queries=game["queries"],
        min_sec=min_sec,
        max_sec=max_sec,
        search_limit=int(game.get("search_limit", 12)),
    )
    pick = pick_candidate(candidates, used)
    if not pick:
        fallback_urls = game.get("fallback_urls") or []
        if fallback_urls:
            fb_min = float(game.get("fallback_min_duration_sec", min_sec))
            logging.info("%s: trying %s fallback url(s)", gid, len(fallback_urls))
            fb = candidates_from_urls(
                fallback_urls,
                env,
                min_sec=fb_min,
                max_sec=max_sec,
                used_ids=used,
            )
            pick = pick_candidate(fb, used)
            if pick:
                report["source"] = "fallback_url"
    if not pick:
        report["skipped"] = "no_candidate"
        logging.warning("no candidate for %s", gid)
        return report

    send_text(
        env,
        chat_id,
        f"🌙 [{game['queue_label']}] качаю YouTube ~{int(pick['duration'] // 60)} мин\n"
        f"{pick['title'][:160]}\n{pick['url']}",
    )

    try:
        source = download_video(pick, env)
    except Exception as exc:
        report["skipped"] = f"download:{exc}"
        logging.exception("download %s", gid)
        return report

    montages = int(game.get("montages", 2))
    ok = 0
    for variant in range(montages):
        if datetime.now(timezone.utc) >= stop_at:
            report["skipped"] = "deadline_mid_game"
            break
        logging.info("%s montage %s/%s", gid, variant + 1, montages)
        skip_mark = variant < montages - 1
        code, tail = run_montage(source, env, chat_id, game, variant, skip_mark_used=skip_mark)
        if code == 0:
            ok += 1
        else:
            logging.error("%s variant %s failed: %s", gid, variant, tail[-400:])
        time.sleep(6)

    report["montages_ok"] = ok
    report["video_id"] = pick["id"]
    report["title"] = pick["title"][:120]
    report["url"] = pick["url"]
    if ok > 0:
        used.add(pick["id"])
        state.setdefault("used_by_game", {})[gid] = list(used)[-80:]
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument(
        "--deadline-hour-msk",
        type=int,
        default=int(os.environ.get("OVERNIGHT_DEADLINE_HOUR_MSK", "8")),
    )
    parser.add_argument("--dry-run", action="store_true", help="Discover only, no download")
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
    env = load_env()
    chat_id = env.get("TG_CHAT_ID", "")
    if not env.get("TG_BOT_TOKEN") or not chat_id:
        logging.error("TG_BOT_TOKEN / TG_CHAT_ID missing")
        return 1

    stop_at = deadline_utc(args.deadline_hour_msk)
    logging.info("deadline UTC %s (08:00 MSK target)", stop_at.isoformat())

    if args.dry_run:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from nightly_youtube_montage import discover_candidates

        for game in games:
            min_sec = float(game["min_duration_sec"])
            max_sec = float(game["max_duration_sec"])
            c = discover_candidates(
                env,
                queries=game["queries"],
                min_sec=min_sec,
                max_sec=max_sec,
                search_limit=5,
            )
            pick = c[0] if c else None
            if not pick and game.get("fallback_urls"):
                fb_min = float(game.get("fallback_min_duration_sec", min_sec))
                fb = candidates_from_urls(
                    game["fallback_urls"],
                    env,
                    min_sec=fb_min,
                    max_sec=max_sec,
                )
                pick = fb[0] if fb else None
            label = pick["title"][:60] if pick else "-"
            src = "fallback" if pick and not c else "search"
            print(game["id"], len(c), src, label)
        return 0

    state = load_state()
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    send_text(
        env,
        chat_id,
        f"🌙 Ночной батч (5 игр): старт {started} МСК, дедлайн ~{args.deadline_hour_msk}:00. "
        "Другие авто-рассылки отключены — только эти нарезки.",
    )

    results: list[dict] = []
    for game in games:
        if datetime.now(timezone.utc) >= stop_at:
            results.append({"game": game["id"], "skipped": "deadline"})
            continue
        results.append(process_game(game, env, chat_id, state, stop_at))

    save_state(state)
    WORK_ROOT.joinpath("last_report.json").write_text(
        json.dumps({"started": started, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [f"🌅 Ночной батч завершён ({started} МСК):"]
    for row in results:
        gid = row.get("game", "?")
        if row.get("skipped"):
            lines.append(f"• {gid}: {row['skipped']}")
        else:
            lines.append(f"• {gid}: {row.get('montages_ok', 0)} нарезок")
    send_text(env, chat_id, "\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
