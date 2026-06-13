#!/usr/bin/env python3
"""Build MLBB montages from NEW short sources only (TikTok, Telegram, YouTube Shorts)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_shorts_montage import run_cycle


def main() -> int:
    chat_id = os.environ.get("TG_CHAT_ID", "")
    token = os.environ.get("TG_BOT_TOKEN", "")
    if not chat_id or not token:
        print("[new-sources] TG_CHAT_ID missing")
        return 1

    max_m = int(os.environ.get("MLBB_SHORTS_PER_CYCLE", "2"))
    result = run_cycle(chat_id=chat_id, token=token, max_montages=max_m)
    if result.get("skipped"):
        print(f"[new-sources] skip: {result.get('reason')} shorts={result.get('short_candidates', 0)}")
        return 0
    print(
        f"[new-sources] ok={result.get('montages_ok')} fail={result.get('montages_fail')} "
        f"shorts={result.get('short_candidates')}"
    )
    return 0 if result.get("montages_ok", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
