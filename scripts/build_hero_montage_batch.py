#!/usr/bin/env python3
"""Build N single-hero montages (e.g. 7 Chou demos) and send to owner Telegram."""

from __future__ import annotations

import json
import os
import random
import subprocess
import tempfile
import time
from pathlib import Path

HERO_ROOT = Path("/root/hero_datasets")
OUTPUT_DIR = Path("/root/videos")
ENV_FILE = Path("/root/.video_bot.env")
STATE_PATH = Path("/root/data/mlbb/hero_montage_batch_state.json")
SMART_EDITOR = Path("/usr/local/bin/smart_video_editor.py")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def build_queue(paths: list[Path], chat_id: str, label: str) -> Path:
    fd, queue_path = tempfile.mkstemp(prefix="hero-batch-", suffix=".txt", dir="/tmp")
    with os.fdopen(fd, "w") as handle:
        for p in paths:
            handle.write(f"{p}|{label}|{chat_id}\n")
    return Path(queue_path)


def pick_sources(hero_id: str, limit: int, prefer_owner: bool = True) -> list[Path]:
    folder = HERO_ROOT / hero_id
    if not folder.is_dir():
        return []
    files = sorted(folder.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if prefer_owner:
        owner = [p for p in files if p.name.startswith("owner_")]
        rest = [p for p in files if not p.name.startswith("owner_")]
        files = owner + rest
    return files[:limit]


def main() -> int:
    load_env_file(ENV_FILE)
    hero = (os.environ.get("HERO_ID") or "chou").strip().lower()
    chat_id = os.environ.get("TG_CHAT_ID", "")
    count = int(os.environ.get("MONTAGE_COUNT", "7"))
    sources_per = int(os.environ.get("SOURCES_PER_MONTAGE", "10"))
    if not chat_id:
        print("TG_CHAT_ID missing")
        return 1

    pool = pick_sources(hero, limit=max(sources_per * 2, 20))
    if len(pool) < 4:
        print(f"not enough sources for {hero}: {len(pool)}")
        return 2

    state: dict = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}

    ok_count = 0
    for idx in range(count):
        random.shuffle(pool)
        batch = pool[: min(sources_per, len(pool))]
        label = f"MLBB {hero.title()} demo {idx + 1}/{count}"
        queue = build_queue(batch, chat_id, label)
        basename = f"mlbb_{hero}_demo_{time.strftime('%Y%m%d')}_{idx + 1:02d}"
        env = os.environ.copy()
        owner_trusted = os.environ.get("OWNER_TRUSTED_SOURCES", "1") == "1"
        env.update(
            {
                "ETALON_MONTAGE": "1" if owner_trusted else "0",
                "QUEUE_FILE": str(queue),
                "OUTPUT_DIR": str(OUTPUT_DIR),
                "OUTPUT_BASENAME": basename,
                "SEND_TELEGRAM": "1",
                "MAX_SOURCES": str(len(batch)),
                "MIN_HIGHLIGHTS": "3",
                "MAX_HIGHLIGHTS": "4",
                "TARGET_DURATION": "45",
                "MIN_FINAL_DURATION": "33",
                "MAX_FINAL_DURATION": "57",
                "SINGLE_HERO_MODE": "1",
                "SINGLE_HERO_ID": hero,
                "SMART_ADD_MUSIC": "0",
                "SMART_GAME_AUDIO_ONLY": "1",
                "SMART_STRIP_MUSIC_BED": "1",
                "SMART_REJECT_TRAINING": "1",
                "SMART_REJECT_MUSIC_BED": "0" if owner_trusted else "1",
                "SMART_REJECT_PROMO": "1",
                "SMART_REJECT_HERO_SHOWCASE": "1",
                "SMART_REQUIRE_UNIFORM_GAMEPLAY": "0" if owner_trusted else "1",
                "BLUR_NICKNAME": "0",
                "STRICT_GAMEPLAY": os.environ.get("STRICT_GAMEPLAY", "0"),
                "SMART_MIN_HUD": "14" if owner_trusted else "15",
                "SMART_MIN_HUD_FRAME_RATE": "0.50" if owner_trusted else "0.55",
                "SMART_MAX_OVERLAY_TEXT": "0.62" if owner_trusted else "0.32",
                "SMART_MAX_REJECT_SIM": "0.995" if owner_trusted else "0.80",
                "SELECTION_VARIANT": str(idx % 7),
            }
        )
        print(f"[batch] montage {idx + 1}/{count} sources={len(batch)} variant={env['SELECTION_VARIANT']}", flush=True)
        env.setdefault("TG_BOT_TOKEN", os.environ.get("TG_BOT_TOKEN", ""))
        env.setdefault("TG_CHAT_ID", chat_id)
        try:
            rc = subprocess.run(["python3", str(SMART_EDITOR)], env=env, check=False).returncode
        finally:
            queue.unlink(missing_ok=True)
        if rc == 0:
            ok_count += 1
        time.sleep(2)

    state[hero] = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "requested": count,
        "ok": ok_count,
        "pool_size": len(pool),
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))

    subprocess.run(
        [
            "curl",
            "-sS",
            "-m",
            "60",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            "text="
            + f"Chou: готово {ok_count}/{count} нарезок из ваших загрузок (пул {len(pool)} mp4). "
            "Напишите номера/что не так — подстроим фильтры.",
            f"https://api.telegram.org/bot{os.environ.get('TG_BOT_TOKEN', '')}/sendMessage",
        ],
        env={k: v for k, v in os.environ.items() if "proxy" not in k.lower()},
        check=False,
    )
    print(f"finished ok={ok_count}/{count}")
    return 0 if ok_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
