#!/usr/bin/env python3
"""Move stuck yt-dlp work files into pending queue (one-off recovery)."""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

# Reuse bot helpers when installed on VPS
sys.path.insert(0, "/usr/local/bin")
from telegram_upload_bot import (  # noqa: E402
    append_pending,
    chat_pending_dir,
    finalize_yt_work_file,
    game_label_for_chat,
    load_env,
)

ENV_FILE = Path("/root/.video_bot.env")


def main() -> int:
    env = load_env(ENV_FILE)
    chat_id = env.get("TG_CHAT_ID", "")
    if not chat_id:
        print("TG_CHAT_ID missing")
        return 1
    pending = chat_pending_dir(chat_id)
    works = sorted(pending.glob("_yt_tmp_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not works:
        print("no _yt_tmp_* dirs")
        return 0
    work = works[0]
    print(f"recover from {work}")
    outfile = finalize_yt_work_file(work)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    destination = pending / f"{stamp}_youtube_{outfile.stem.replace('yt_', '')}.mp4"
    shutil.move(str(outfile), str(destination))
    shutil.rmtree(work, ignore_errors=True)
    append_pending(chat_id, destination, game_label_for_chat(chat_id))
    print(f"ok {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
