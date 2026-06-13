#!/usr/bin/env python3
"""Continuous MLBB shorts queue — short sources only, auto-send to owner."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_shorts_montage import run_cycle

ENV_FILE = Path("/root/.video_bot.env")
LOG = Path("/root/data/mlbb/mlbb_shorts_pipeline.log")
PAUSE_FILE = Path("/root/data/mlbb/PAUSED_PIPELINES")


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


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line)
    print(line, end="")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--montages", type=int, default=int(os.environ.get("MLBB_SHORTS_PER_CYCLE", "3")))
    args = parser.parse_args()

    if PAUSE_FILE.exists() and "mlbb_shorts_pipeline.py" in PAUSE_FILE.read_text():
        log("paused by PAUSED_PIPELINES")
        return 0

    env = load_env()
    chat_id = env.get("TG_CHAT_ID", "")
    token = env.get("TG_BOT_TOKEN", "")
    if not chat_id or not token:
        log("REFUSED: missing TG env")
        return 1

    for key, val in env.items():
        os.environ.setdefault(key, val)

    log(f"mlbb_shorts cycle start montages={args.montages}")
    result = run_cycle(chat_id=chat_id, token=token, max_montages=args.montages)
    log(
        f"done ok={result.get('montages_ok', 0)} fail={result.get('montages_fail', 0)} "
        f"shorts={result.get('short_candidates', 0)} reason={result.get('reason', '')}"
    )
    return 0 if result.get("montages_ok") or result.get("skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
