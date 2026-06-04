#!/usr/bin/env python3
"""Proactive: pick 2h+ MLBB YouTube VOD, download (no proxy), build 3 Smart Edit montages."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ENV_FILE = Path("/root/.video_bot.env")
PROCESSOR = Path("/usr/local/bin/smart_video_editor.py")
OUTPUT_DIR = Path("/root/videos")
WORK_ROOT = Path("/root/data/mlbb/youtube_proactive")
LOG_FILE = WORK_ROOT / "proactive.log"


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
    out = env.copy()
    for key in list(out):
        if "proxy" in key.lower():
            out.pop(key, None)
    return out


def run_montage(
    source: Path,
    env: dict[str, str],
    chat_id: str,
    variant: int,
    *,
    skip_mark_used: bool,
) -> tuple[int, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from montage_env import profile_montage_env

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".queue.txt") as tmp:
        tmp.write(f"{source.resolve()}|MLBB|{chat_id}\n")
        queue_path = tmp.name

    run_env = clean_proxy_env(os.environ.copy())
    for key, value in env.items():
        run_env.setdefault(key, value)
    run_env.update(profile_montage_env("mobile_legends"))
    run_env.update(
        {
            "QUEUE_FILE": queue_path,
            "MAX_SOURCES": "1",
            "SINGLE_SOURCE_MODE": "1",
            "SEND_TELEGRAM": "1",
            "TG_CHAT_ID": chat_id,
            "TG_BOT_TOKEN": env.get("TG_BOT_TOKEN", ""),
            "OUTPUT_DIR": str(OUTPUT_DIR),
            "DEFAULT_GAME_PROFILE": "mobile_legends",
            "QUEUE_GAME_PROFILE": "mobile_legends",
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
        tail = (completed.stderr or completed.stdout or "")[-1200:]
        return completed.returncode, tail
    finally:
        Path(queue_path).unlink(missing_ok=True)


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="Use this YouTube URL instead of search")
    parser.add_argument("--montages", type=int, default=3)
    parser.add_argument("--skip-download", type=Path, help="Existing mp4 on disk")
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

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from nightly_youtube_montage import (  # noqa: WPS433
        discover_candidates,
        download_video,
        fetch_video_meta,
        pick_candidate,
    )

    env = load_env()
    chat_id = env.get("TG_CHAT_ID", "")
    if not env.get("TG_BOT_TOKEN") or not chat_id:
        logging.error("TG_BOT_TOKEN / TG_CHAT_ID missing")
        return 1

    env.setdefault("YOUTUBE_NIGHT_MIN_SEC", "7200")
    env.setdefault("YOUTUBE_NIGHT_MAX_SEC", "14400")

    import json

    state_path = WORK_ROOT / "state.json"
    state: dict = {"used_ids": []}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    used = set(state.get("used_ids") or [])

    if args.skip_download:
        source = args.skip_download
        pick = {"id": source.stem, "title": source.name, "url": "", "duration": 0}
    elif args.url:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(args.url.strip())
        vid = ""
        if parsed.netloc.endswith("youtu.be"):
            vid = parsed.path.strip("/").split("/")[0][:11]
        else:
            vid = (parse_qs(parsed.query).get("v") or [""])[0][:11]
        meta = fetch_video_meta(vid, env)
        if not meta:
            logging.error("meta failed for %s", args.url)
            return 1
        if meta["duration"] < 7200:
            logging.warning("video %.0f sec < 2h", meta["duration"])
        pick = meta
    else:
        candidates = discover_candidates(env)
        pick = pick_candidate(candidates, used)
        if not pick:
            send_text(env, chat_id, "📺 YouTube: не нашёл новый VOD 2+ ч. Повторим позже.")
            return 0

    send_text(
        env,
        chat_id,
        f"📺 Проактивно: качаю MLBB VOD (~{int(pick.get('duration', 0) // 60)} мин)\n"
        f"{pick.get('title', '')[:180]}\n{pick.get('url', '')}",
    )

    if not args.skip_download:
        try:
            source = download_video(pick, env)
        except Exception as exc:
            logging.exception("download failed")
            send_text(env, chat_id, f"📺 YouTube не скачался: {exc}")
            return 1
    else:
        source = args.skip_download

    logging.info("source=%s", source)
    send_text(
        env,
        chat_id,
        f"📺 Скачано. Делаю {args.montages} нарезки (звук без DSP-фильтра)…",
    )

    ok = 0
    last_tail = ""
    for variant in range(args.montages):
        skip_mark = variant < args.montages - 1
        logging.info("montage %s/%s variant=%s", variant + 1, args.montages, variant)
        code, last_tail = run_montage(source, env, chat_id, variant, skip_mark_used=skip_mark)
        if code == 0:
            ok += 1
        else:
            logging.error("montage variant %s failed: %s", variant, last_tail[-400:])
        time.sleep(8)

    if pick.get("id"):
        used.add(pick["id"])
        state["used_ids"] = list(used)[-200:]
        state_path.write_text(
            __import__("json").dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if ok:
        send_text(
            env,
            chat_id,
            f"📺 Готово: {ok}/{args.montages} нарезок из YouTube VOD.\n"
            f"{pick.get('url', source.name)}",
        )
        return 0

    send_text(
        env,
        chat_id,
        f"📺 Нарезки не собрались (0/{args.montages}). Источник сохранён.\n{last_tail[-500:]}",
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
