#!/usr/bin/env python3
"""Copy owner pending Telegram uploads into hero_datasets/<hero>/."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

PENDING_ROOT = Path("/root/telegram_uploads/pending")
HERO_ROOT = Path("/root/hero_datasets")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("chat_id", help="Telegram chat id folder under pending/")
    parser.add_argument("hero_id", help="hero_datasets subfolder, e.g. chou")
    parser.add_argument("--prefix", default="owner_", help="filename prefix")
    args = parser.parse_args()

    pending = PENDING_ROOT / args.chat_id
    dest_dir = HERO_ROOT / args.hero_id.lower()
    if not pending.is_dir():
        print(f"no pending dir: {pending}")
        return 1
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in sorted(pending.glob("*.mp4")):
        target = dest_dir / f"{args.prefix}{src.name}"
        if target.exists() and target.stat().st_size == src.stat().st_size:
            continue
        shutil.copy2(src, target)
        copied += 1
        print(f"copied {src.name} -> {target.name}")
    total = len(list(dest_dir.glob("*.mp4")))
    print(f"done copied={copied} total_in_{args.hero_id}={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
