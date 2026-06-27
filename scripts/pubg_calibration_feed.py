#!/usr/bin/env python3
"""Send PUBG Shorts to owner for 👍/👎 calibration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pubg_shorts_calibration_store import (
    claim_feed_candidates,
    feed_singleton_lock,
    inline_keyboard_markup,
    mark_feed_blocked,
    mark_feed_sent,
    pending_candidates,
    rebuild_index_from_disk,
    release_feed_claims,
    release_stale_claims,
    repair_index,
    stats,
)
from pubg_shorts_title_gate import pubg_short_passes_calibration
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
DATA_PUBG = Path(os.environ.get("SHOOTER_PUBG_DATA_ROOT", "/root/data/pubg"))
EMPTY_NOTIFY_PATH = DATA_PUBG / "calibration_feed_empty_notify.json"


def send_message(token: str, chat_id: str, text: str, *, video_id: str = "") -> None:
    cmd = ["curl", "-sS", "-F", f"chat_id={chat_id}", "-F", f"text={text[:3900]}"]
    if video_id:
        cmd.extend(
            ["-F", f"reply_markup={json.dumps(inline_keyboard_markup(video_id), ensure_ascii=False)}"]
        )
    cmd.append(f"https://api.telegram.org/bot{token}/sendMessage")
    subprocess.run(cmd, check=False, timeout=30)


def format_caption(row: dict, idx: int, total: int) -> str:
    vid = row.get("video_id", "")
    hint = str(row.get("gameplay_reason") or "")
    metro = "Metro ✓" if hint == "metro" else ("Classic?" if hint.startswith("non_metro") else hint[:40])
    return (
        f"PUBG калибровка {idx}/{total}\n"
        f"{metro} | score={float(row.get('gameplay_score', 0)):.2f}\n"
        f"views={int(row.get('view_count') or 0)}\n"
        f"{row.get('title', '')[:120]}\n"
        f"{row.get('url', '')}\n"
        f"#id {vid}\n"
        f"👍 Metro/бой ок · 👎 не Metro / не бой"
    )


def send_video(token: str, chat_id: str, path: Path, caption: str, *, video_id: str = "") -> bool:
    from mlbb_telegram_video import send_calibration_video

    markup = inline_keyboard_markup(video_id) if video_id else None
    return send_calibration_video(token, chat_id, path, caption, reply_markup=markup)


def _pick_batch(rows: list[dict], batch_size: int) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        vid = str(row.get("video_id", ""))
        path = Path(row.get("path", ""))
        if not vid or vid in seen or path.name != f"yt_{vid}.mp4":
            continue
        seen.add(vid)
        out.append(row)
        if len(out) >= batch_size:
            break
    return out


def main() -> int:
    with feed_singleton_lock() as acquired:
        if not acquired:
            print("skip feed another instance running")
            return 0
        return _run_feed()


def _run_feed() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    batch_size = int(env.get("PUBG_CALIBRATION_BATCH", "5"))
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1

    repair_index()
    rebuild_index_from_disk()
    stale = release_stale_claims(max_age_sec=float(env.get("PUBG_CLAIM_STALE_SEC", "300")))
    if stale:
        print(f"released_stale_claims={stale}")

    picked = claim_feed_candidates(
        _pick_batch(pending_candidates(limit=max(batch_size * 4, 20), repair=False), batch_size)
    )
    if not picked:
        s = stats()
        print(f"empty_queue pending={s['pending']} delivered={s['delivered']}")
        return 0

    sent_ids: list[str] = []
    failed_ids: list[str] = []
    for idx, row in enumerate(picked, start=1):
        path = Path(row.get("path", ""))
        vid = str(row.get("video_id", ""))
        if not path.exists():
            failed_ids.append(vid)
            print(f"skip missing file video_id={vid}")
            continue
        ok, gscore, reason = pubg_short_passes_calibration(path, title=str(row.get("title", "")))
        if not ok:
            mark_feed_blocked(vid, reason=reason, score=gscore)
            failed_ids.append(vid)
            print(f"skip gate video_id={vid} reason={reason}")
            continue
        caption = format_caption(row, idx, len(picked))
        if not send_video(token, chat_id, path, caption, video_id=vid):
            failed_ids.append(vid)
            print(f"skip telegram_fail video_id={vid} size={path.stat().st_size}")
            continue
        sent_ids.append(vid)
        mark_feed_sent([vid], paths=[path])
        time.sleep(float(env.get("PUBG_SHORTS_SEND_DELAY_SEC", "1.5")))

    if failed_ids:
        release_feed_claims(failed_ids)
    s = stats()
    print(f"feed done sent={len(sent_ids)} failed={len(failed_ids)} delivered={s['delivered']}")
    return 0 if sent_ids else 1


if __name__ == "__main__":
    raise SystemExit(main())
