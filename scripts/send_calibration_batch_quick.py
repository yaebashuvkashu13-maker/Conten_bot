#!/usr/bin/env python3
"""Fast calibration batch: skip slow per-frame gates; owner labels results."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERO_ROOT = Path("/root/hero_datasets")
ENV_PATH = Path("/root/.video_bot.env")
STATE_PATH = Path("/root/data/mlbb/calibration_batch_sent.json")
BATCH_NUM = int(os.environ.get("CALIBRATION_BATCH_NUM", "2"))
BATCH_SIZE = 15
PREFERRED = {"moskov", "franco", "miya", "chou", "valentina"}


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def ffprobe_duration(path: Path) -> float:
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=25,
    )
    try:
        return float((r.stdout or "0").strip())
    except ValueError:
        return 0.0


def send_video(token: str, chat_id: str, path: Path, caption: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    clean = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    r = subprocess.run(
        [
            "curl",
            "-sS",
            "-m",
            "600",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            "supports_streaming=true",
            "-F",
            f"caption={caption[:900]}",
            "-F",
            f"video=@{path}",
            url,
        ],
        capture_output=True,
        text=True,
        env=clean,
        timeout=620,
    )
    try:
        return bool(json.loads(r.stdout).get("ok"))
    except json.JSONDecodeError:
        return False


def main() -> int:
    sys.path.insert(0, "/usr/local/bin")
    from gameplay_gate import (  # noqa: WPS433
        heuristic_gameplay_score,
        path_blocked_by_calibration,
        profile_looks_like_mlbb_edit,
    )

    env = load_env(ENV_PATH)
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        return 1

    sent_before: set[str] = set()
    if STATE_PATH.exists():
        sent_before = set(json.loads(STATE_PATH.read_text()).get("paths", []))

    rows: list[tuple[float, Path, str]] = []
    for hero_dir in sorted(HERO_ROOT.iterdir()):
        if not hero_dir.is_dir():
            continue
        hero = hero_dir.name
        for path in hero_dir.glob("*.mp4"):
            if str(path) in sent_before or path_blocked_by_calibration(path):
                continue
            dur = ffprobe_duration(path)
            if dur < 18 or dur > 75:
                continue
            if profile_looks_like_mlbb_edit(path, 4):
                continue
            score = heuristic_gameplay_score(path, 4)
            if score < 0.50:
                continue
            bonus = 0.15 if hero in PREFERRED else 0.0
            rows.append((score + bonus, path, hero))

    rows.sort(reverse=True)
    picked: list[tuple[Path, str]] = []
    used_heroes: set[str] = set()
    for _rank, path, hero in rows:
        if len(picked) >= BATCH_SIZE:
            break
        if hero in used_heroes and len(used_heroes) < 10:
            continue
        picked.append((path, hero))
        used_heroes.add(hero)
    for _rank, path, hero in rows:
        if len(picked) >= BATCH_SIZE:
            break
        if path in {p for p, _ in picked}:
            continue
        picked.append((path, hero))

    if len(picked) < BATCH_SIZE:
        print(f"only {len(picked)} candidates", flush=True)
        return 1

    intro = (
        f"Калибровка партия {BATCH_NUM}: {len(picked)} сырых TikTok. "
        "Нумерация 1–15. Ответьте, где НЕ геймплей: «3, 7 — удалить»."
    )
    clean = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    subprocess.run(
        [
            "curl",
            "-sS",
            "-m",
            "60",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"text={intro}",
            f"https://api.telegram.org/bot{token}/sendMessage",
        ],
        env=clean,
        check=False,
    )

    sent_new: list[str] = []
    for idx, (path, hero) in enumerate(picked, start=1):
        cap = f"партия{BATCH_NUM} · {idx}/15 | {hero} | id:{path.stem}"
        ok = send_video(token, chat_id, path, cap)
        print(f"[{idx}] {hero} {path.name} ok={ok}", flush=True)
        if ok:
            sent_new.append(str(path))
        time.sleep(1.1)

    STATE_PATH.write_text(
        json.dumps(
            {"paths": list(sent_before) + sent_new, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
