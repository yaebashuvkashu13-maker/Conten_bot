#!/usr/bin/env python3
"""Background Instagram digest prep (runs parallel to TikTok download)."""

from __future__ import annotations

import json
import time
from pathlib import Path

STATE = Path("/root/data/mlbb/instagram_worker_state.json")
CONFIG = Path("/root/config.instagram-mlbb.yaml")
if not CONFIG.exists():
    CONFIG = Path("/workspace/config.instagram-mlbb.yaml")


def main() -> int:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_tick": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config_exists": CONFIG.exists(),
        "ad_examples_count": len(list(Path("/root/data/mlbb/ad_examples").glob("*")))
        if Path("/root/data/mlbb/ad_examples").exists()
        else 0,
        "note": "Full digest needs cookies on VPS; ad screenshots -> /root/data/mlbb/ad_examples/",
    }
    if CONFIG.exists():
        payload["bloggers"] = CONFIG.read_text(encoding="utf-8").count("instagram.com/")
    STATE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
