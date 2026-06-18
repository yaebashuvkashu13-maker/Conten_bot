#!/usr/bin/env python3
"""Publish up to 3 queued MLBB clips to VK (scheduled slots MSK)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vk_mlbb_queue import (
    BATCH_SIZE,
    append_publish_log,
    archive_published,
    pending_count,
    pop_batch,
)
from vk_mlbb_upload import load_env, publish_clip, publish_short_clip

ENV_FILE = Path("/root/.video_bot.env")
SLOT_LABELS = {
    "morning": "09:00 МСК",
    "afternoon": "13:30 МСК",
    "evening": "18:00 МСК",
}


def notify_telegram(text: str) -> None:
    env = load_env()
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        append_publish_log(f"notify_skip no_telegram: {text}")
        return
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=60) as resp:
            resp.read()
    except Exception as exc:
        append_publish_log(f"notify_fail: {exc}")


def main() -> int:
    if os.environ.get("VK_MLBB_DISABLED", "0") == "1":
        print("VK MLBB publish disabled (VK_MLBB_DISABLED=1)")
        return 0

    parser = argparse.ArgumentParser()
    parser.add_argument("slot", choices=["morning", "afternoon", "evening"])
    args = parser.parse_args()
    slot_label = SLOT_LABELS[args.slot]

    batch = pop_batch(BATCH_SIZE)
    if not batch:
        msg = f"VK MLBB {slot_label}: очередь пуста — нечего заливать. Жду /upload_vkmlbb."
        append_publish_log(msg)
        if os.environ.get("VK_MLBB_NOTIFY_EMPTY", "1") == "1":
            notify_telegram(msg)
        print(msg)
        return 0

    published = 0
    errors: list[str] = []
    env = load_env()

    for path in batch:
        try:
            meta = {}
            meta_path = path.with_suffix(".json")
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            publish_fn = publish_short_clip if os.environ.get("VK_MLBB_SHORT_VIDEO", "1") == "1" else publish_clip
            result = publish_fn(path, title=meta.get("label") or path.stem, env=env)
            vid = result.get("video_id", "")
            archive_published(path, vk_video_id=str(vid), slot=args.slot)
            published += 1
            append_publish_log(f"OK {args.slot} {path.name} video_id={vid}")
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
            append_publish_log(f"FAIL {args.slot} {path.name}: {exc}")

    left = pending_count()
    lines = [
        f"VK MLBB {slot_label}: залито {published}/{len(batch)}.",
        f"В очереди осталось: {left}.",
    ]
    if errors:
        lines.append("Ошибки:")
        lines.extend(errors[:5])
    msg = "\n".join(lines)
    notify_telegram(msg)
    print(msg)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
