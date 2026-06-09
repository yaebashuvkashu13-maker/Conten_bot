#!/usr/bin/env python3
"""Send top unevaluated MLBB Shorts candidates to owner for yes/no calibration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_calibration_store import DATA_MLBB, mark_feed_sent, pending_candidates, stats
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
BATCH_SIZE = int(os.environ.get("MLBB_CALIBRATION_BATCH", "3"))
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024
QUIET_EMPTY_SEC = int(os.environ.get("MLBB_FEED_QUIET_EMPTY_SEC", "21600"))  # 6h
EMPTY_NOTIFY_PATH = DATA_MLBB / "calibration_feed_empty_notify.json"


def send_video(token: str, chat_id: str, path: Path, caption: str) -> bool:
    if path.stat().st_size > TELEGRAM_MAX_BYTES:
        return False
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    cmd = [
        "curl",
        "-sS",
        "-m",
        "600",
        "-F",
        f"chat_id={chat_id}",
        "-F",
        "supports_streaming=true",
        "-F",
        f"caption={caption[:900]}",
        "-F",
        f"video=@{path}",
        url,
    ]
    clean_env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    result = subprocess.run(cmd, capture_output=True, text=True, env=clean_env, timeout=620)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return bool(payload.get("ok"))


def send_message(token: str, chat_id: str, text: str) -> None:
    subprocess.run(
        [
            "curl",
            "-sS",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"text={text[:3900]}",
            f"https://api.telegram.org/bot{token}/sendMessage",
        ],
        env={k: v for k, v in os.environ.items() if "proxy" not in k.lower()},
        check=False,
        timeout=30,
    )


def format_caption(row: dict, idx: int, total: int) -> str:
    vid = row.get("video_id", "")
    return (
        f"MLBB калибровка {idx}/{total}\n"
        f"score={float(row.get('score', 0)):.3f} | hook={float(row.get('hook_score', 0)):.2f}\n"
        f"views={int(row.get('view_count') or 0)}\n"
        f"{row.get('title', '')[:120]}\n"
        f"{row.get('url', '')}\n"
        f"#id {vid}\n"
        f"👍 /mlbb_yes {vid}\n"
        f"👎 /mlbb_no {vid} причина"
    )


def main() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID") or env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1

    picked = pending_candidates(limit=BATCH_SIZE)
    if not picked:
        s = stats()
        now = time.time()
        last_notify = 0.0
        if EMPTY_NOTIFY_PATH.exists():
            try:
                last_notify = float(json.loads(EMPTY_NOTIFY_PATH.read_text()).get("at", 0))
            except (json.JSONDecodeError, ValueError, OSError):
                last_notify = 0.0
        if now - last_notify >= QUIET_EMPTY_SEC:
            send_message(
                token,
                chat_id,
                "MLBB калибровка: очередь пуста — ждём ingest с YouTube.\n"
                f"Индекс: {s['index_total']}, в очереди: {s['pending']}.\n"
                "Ingest идёт ~раз в 3ч (бережно к YouTube).",
            )
            EMPTY_NOTIFY_PATH.parent.mkdir(parents=True, exist_ok=True)
            EMPTY_NOTIFY_PATH.write_text(json.dumps({"at": now}), encoding="utf-8")
        else:
            print(f"skip empty notify pending={s['pending']} quiet={QUIET_EMPTY_SEC}s")
        return 0

    send_message(
        token,
        chat_id,
        f"MLBB Shorts — {len(picked)} кандидатов на оценку.\n"
        "Ответь: /mlbb_yes {id} или /mlbb_no {id} причина\n"
        f"Статистика: 👍{stats()['feedback_yes']} 👎{stats()['feedback_no']}",
    )

    sent_ids: list[str] = []
    for idx, row in enumerate(picked, start=1):
        path = Path(row.get("path", ""))
        if not path.exists():
            continue
        caption = format_caption(row, idx, len(picked))
        ok = send_video(token, chat_id, path, caption)
        if not ok:
            vid = str(row.get("video_id", ""))
            send_message(
                token,
                chat_id,
                f"#{idx} (видео >20MB, ссылка)\n{caption}",
            )
        sent_ids.append(str(row.get("video_id", "")))
        time.sleep(1.2)

    mark_feed_sent(sent_ids)
    print(f"sent={len(sent_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
