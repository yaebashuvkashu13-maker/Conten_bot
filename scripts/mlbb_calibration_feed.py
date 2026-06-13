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

from mlbb_calibration_store import (
    DATA_MLBB,
    inline_keyboard_markup,
    mark_feed_sent,
    pending_candidates,
    rebuild_index_from_disk,
    repair_index,
    stats,
)
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
BATCH_SIZE = int(os.environ.get("MLBB_CALIBRATION_BATCH", "3"))
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024
QUIET_EMPTY_SEC = int(os.environ.get("MLBB_FEED_QUIET_EMPTY_SEC", "7200"))  # 2h
EMPTY_NOTIFY_PATH = DATA_MLBB / "calibration_feed_empty_notify.json"


def send_video(
    token: str,
    chat_id: str,
    path: Path,
    caption: str,
    *,
    video_id: str = "",
) -> bool:
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
    if video_id:
        cmd.insert(-1, "-F")
        cmd.insert(-1, f"reply_markup={json.dumps(inline_keyboard_markup(video_id), ensure_ascii=False)}")
    clean_env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    result = subprocess.run(cmd, capture_output=True, text=True, env=clean_env, timeout=620)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return bool(payload.get("ok"))


def send_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    video_id: str = "",
) -> None:
    cmd = [
        "curl",
        "-sS",
        "-F",
        f"chat_id={chat_id}",
        "-F",
        f"text={text[:3900]}",
    ]
    if video_id:
        cmd.extend(
            [
                "-F",
                f"reply_markup={json.dumps(inline_keyboard_markup(video_id), ensure_ascii=False)}",
            ]
        )
    cmd.append(f"https://api.telegram.org/bot{token}/sendMessage")
    subprocess.run(
        cmd,
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
        f"Нажми 👍 или 👎 под видео"
    )


def main() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID") or env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1

    repair_index()
    rebuild_index_from_disk()
    picked = pending_candidates(limit=max(BATCH_SIZE * 3, 12))
    # Guarantee unique files — never send the same mp4 twice in one batch.
    unique: list[dict] = []
    seen_paths: set[str] = set()
    seen_vids: set[str] = set()
    for row in picked:
        vid = str(row.get("video_id", ""))
        path = Path(row.get("path", ""))
        if not vid or vid in seen_vids:
            continue
        path_key = str(path.resolve()) if path.exists() else ""
        if not path_key or path_key in seen_paths:
            continue
        if path.name != f"yt_{vid}.mp4":
            continue
        seen_vids.add(vid)
        seen_paths.add(path_key)
        unique.append(row)
        if len(unique) >= BATCH_SIZE:
            break
    picked = unique
    if not picked:
        s = stats()
        if s["pending"] == 0 and os.environ.get("MLBB_FEED_TRY_INGEST", "1") == "1":
            ingest = Path("/usr/local/bin/mlbb_youtube_shorts_ingest.py")
            if not ingest.exists():
                ingest = Path(__file__).resolve().parent / "mlbb_youtube_shorts_ingest.py"
            subprocess.run(
                [
                    sys.executable,
                    str(ingest),
                    "--incremental",
                    "--max-downloads",
                    "8",
                    "--max-per-query",
                    "20",
                ],
                env={**env, "MLBB_INGEST_SKIP_IF_PENDING": "0"},
                timeout=600,
                check=False,
            )
            rebuild_index_from_disk()
            picked = pending_candidates(limit=max(BATCH_SIZE * 3, 12))
            s = stats()

    if not picked:
        now = time.time()
        last_notify = 0.0
        if EMPTY_NOTIFY_PATH.exists():
            try:
                last_notify = float(json.loads(EMPTY_NOTIFY_PATH.read_text()).get("at", 0))
            except (json.JSONDecodeError, ValueError, OSError):
                last_notify = 0.0
        s = stats()
        if now - last_notify >= QUIET_EMPTY_SEC:
            send_message(
                token,
                chat_id,
                "MLBB калибровка: очередь пуста — ingest ищет новые Shorts.\n"
                f"Индекс: {s['index_total']}, в очереди: {s['pending']}.\n"
                "Continuous worker качает и режет VOD параллельно.",
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
        "Под каждым роликом — кнопки 👍 / 👎\n"
        f"Статистика: 👍{stats()['feedback_yes']} 👎{stats()['feedback_no']}",
    )

    sent_ids: list[str] = []
    for idx, row in enumerate(picked, start=1):
        path = Path(row.get("path", ""))
        if not path.exists():
            continue
        caption = format_caption(row, idx, len(picked))
        vid = str(row.get("video_id", ""))
        ok = send_video(token, chat_id, path, caption, video_id=vid)
        if not ok:
            send_message(
                token,
                chat_id,
                f"#{idx} (видео >20MB, ссылка)\n{caption}",
                video_id=vid,
            )
        sent_ids.append(str(row.get("video_id", "")))
        time.sleep(1.2)

    mark_feed_sent(
        sent_ids,
        paths=[Path(row.get("path", "")) for row in picked if row.get("path")],
    )
    print(f"sent={len(sent_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
