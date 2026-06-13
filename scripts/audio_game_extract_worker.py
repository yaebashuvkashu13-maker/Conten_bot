#!/usr/bin/env python3
"""Extract game audio stems queue (parallel to downloads)."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

INBOX = Path("/root/datasets/tiktok/mlbb")
OUT = Path("/root/datasets/audio/game_wav")
STATE = Path("/root/data/mlbb/audio_worker_state.json")
BATCH = 20


def extract_wav(video: Path, wav: Path) -> bool:
    wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        wav,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        return wav.exists()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def main() -> int:
    processed = set()
    if STATE.exists():
        processed = set(json.loads(STATE.read_text()).get("done", []))

    done_now: list[str] = []
    for video in sorted(INBOX.rglob("*.mp4")):
        if len(done_now) >= BATCH:
            break
        key = str(video)
        if key in processed:
            continue
        wav = OUT / video.relative_to(INBOX).with_suffix(".wav")
        if extract_wav(video, wav):
            done_now.append(key)

    processed.update(done_now)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps(
            {
                "done_count": len(processed),
                "last_batch": len(done_now),
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            indent=2,
        )
    )
    print(f"audio batch extracted: {len(done_now)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
