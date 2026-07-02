#!/usr/bin/env python3
"""Daily owner status: sends, precision_7d, worker health — no silent idle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_learning_first import (
    daily_send_count,
    enabled,
    max_daily_sends,
    precision_7d,
    sends_allowed,
)
from mlbb_vod_segment_store import stats as vseg_stats


def send_telegram(text: str) -> bool:
    token = os.environ.get("TG_BOT_TOKEN", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            return bool(body.get("ok"))
    except Exception:
        return False


def worker_running() -> bool:
    r = subprocess.run(["pgrep", "-f", "mlbb_continuous_worker.py"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def main() -> int:
    vs = vseg_stats()
    prec = precision_7d()
    sent_today = daily_send_count()
    cap = max_daily_sends()
    lf = "ON" if enabled() else "OFF"
    sends = "да" if sends_allowed() else "нет (gate)"

    text = (
        "📋 MLBB Bot — дневной статус\n"
        f"Worker: {'✅ жив' if worker_running() else '❌ мёртв'}\n"
        f"Отправки сегодня: {sent_today}/{cap}\n"
        f"precision_7d: {prec:.0%} (цель 45%)\n"
        f"Метки VOD: 👍{vs['feedback_yes']} 👎{vs['feedback_no']}\n"
        f"hook_min: {os.environ.get('VIRAL_MLBB_HOOK_MIN', '?')} | batch до {os.environ.get('MLBB_VOD_BATCH_MAX', '30')}\n"
        f"Variable fight cuts: {os.environ.get('MLBB_VOD_VARIABLE_LENGTH', '1')}\n"
        f"LEARNING_FIRST: {lf} | sendVideo: {sends}\n"
        "Silver: YouTube Shorts → CLIP + hook порог каждые 6 мин"
    )
    print(text)
    if "--telegram" in sys.argv and send_telegram(text):
        print("telegram_sent=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
