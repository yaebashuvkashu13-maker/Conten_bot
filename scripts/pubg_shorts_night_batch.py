#!/usr/bin/env python3
"""Overnight: ingest + send PUBG Shorts until target delivered to owner."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pubg_calibration_feed import main as run_feed
from pubg_shorts_calibration_store import load_ever_delivered, stats
from pubg_youtube_shorts_ingest import main as run_ingest
from youtube_download import load_env

LOG = Path("/root/data/pubg/logs/pubg_shorts_night_batch.log")


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def main() -> int:
    env = {**os.environ, **load_env()}
    os.environ.update(env)
    target = int(env.get("PUBG_SHORTS_NIGHT_TARGET", "100"))
    feed_batch = int(env.get("PUBG_CALIBRATION_BATCH", "5"))
    ingest_batch = int(env.get("PUBG_INGEST_MAX_DOWNLOADS", "20"))
    max_rounds = int(env.get("PUBG_SHORTS_NIGHT_MAX_ROUNDS", "80"))
    sleep_sec = float(env.get("PUBG_SHORTS_NIGHT_SLEEP_SEC", "25"))

    start_delivered = len(load_ever_delivered())
    log(f"night batch start target={target} already_delivered={start_delivered}")

    if start_delivered >= target:
        log("target already reached")
        return 0

    rounds = 0
    while rounds < max_rounds:
        delivered = len(load_ever_delivered())
        if delivered >= target:
            log(f"done delivered={delivered}")
            break
        need = target - delivered
        os.environ["PUBG_INGEST_HUNGRY"] = "1" if need > 30 else "0"
        os.environ["PUBG_INGEST_MAX_DOWNLOADS"] = str(min(ingest_batch, max(5, need // 2)))
        os.environ["PUBG_CALIBRATION_BATCH"] = str(min(feed_batch, max(1, min(need, feed_batch))))

        log(f"round={rounds + 1} need={need} ingest_max={os.environ['PUBG_INGEST_MAX_DOWNLOADS']}")
        ingest_rc = run_ingest()
        feed_rc = run_feed()
        s = stats()
        log(
            f"round={rounds + 1} ingest_rc={ingest_rc} feed_rc={feed_rc} "
            f"delivered={s['delivered']} pending={s['pending']} 👍{s['feedback_yes']} 👎{s['feedback_no']}"
        )
        if s["delivered"] >= target:
            break
        if feed_rc != 0 and ingest_rc != 0 and s["pending"] == 0:
            log("stuck: no pending and ingest failed — extend search")
            os.environ["PUBG_INGEST_HUNGRY"] = "1"
            os.environ["PUBG_INGEST_MAX_DOWNLOADS"] = str(ingest_batch * 2)
        rounds += 1
        time.sleep(sleep_sec)

    delivered = len(load_ever_delivered())
    s = stats()
    log(f"finished delivered={delivered}/{target} rounds={rounds} stats={s}")

    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if token and chat_id and delivered > start_delivered:
        import json
        import subprocess as sp

        text = (
            f"🌙 PUBG Shorts калибровка: отправлено {delivered - start_delivered} новых "
            f"(всего {delivered}).\n"
            f"Поставь 👍/👎 под каждым — это обучит Metro/combat gate.\n"
            f"Осталось без оценки: жди следующую порцию или размечай по мере прихода."
        )
        sp.run(
            ["curl", "-sS", "-F", f"chat_id={chat_id}", "-F", f"text={text}", f"https://api.telegram.org/bot{token}/sendMessage"],
            check=False,
            timeout=30,
        )
    return 0 if delivered >= min(target, start_delivered + 1) else 1


if __name__ == "__main__":
    raise SystemExit(main())
