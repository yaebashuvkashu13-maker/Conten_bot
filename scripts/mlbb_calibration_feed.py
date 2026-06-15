#!/usr/bin/env python3
"""Send top unevaluated MLBB Shorts candidates to owner for yes/no calibration."""

from __future__ import annotations

import fcntl
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
    is_stub_candidate,
    mark_feed_sent,
    pending_candidates,
    rebuild_index_from_disk,
    reject_candidate,
    stats,
)
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
BATCH_SIZE = int(os.environ.get("MLBB_CALIBRATION_BATCH", "6"))
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024
QUIET_EMPTY_SEC = int(os.environ.get("MLBB_FEED_QUIET_EMPTY_SEC", "7200"))  # 2h
EMPTY_NOTIFY_PATH = DATA_MLBB / "calibration_feed_empty_notify.json"
LOCK_PATH = DATA_MLBB / "calibration_feed.lock"


def send_video(
    token: str,
    chat_id: str,
    path: Path,
    caption: str,
    *,
    video_id: str = "",
) -> bool:
    from mlbb_learning_first import can_send, record_send

    ok_send, reason = can_send(1)
    if not ok_send:
        print(f"send blocked video_id={video_id} reason={reason}")
        return False
    if path.stat().st_size > TELEGRAM_MAX_BYTES:
        return False
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    cmd = [
        "curl",
        "-sS",
        "-m",
        "120",
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
    result = subprocess.run(cmd, capture_output=True, text=True, env=clean_env, timeout=130)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    ok = bool(payload.get("ok"))
    if ok:
        record_send(1)
    return ok


def send_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    video_id: str = "",
) -> bool:
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
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if "proxy" not in k.lower()},
        check=False,
        timeout=30,
    )
    try:
        return bool(json.loads(result.stdout or "{}").get("ok"))
    except json.JSONDecodeError:
        return False


def format_caption(row: dict, idx: int, total: int, *, header: str = "") -> str:
    vid = row.get("video_id", "")
    prefix = f"{header}\n" if header else ""
    return (
        f"{prefix}MLBB калибровка {idx}/{total}\n"
        f"score={float(row.get('score', 0)):.3f} | hook={float(row.get('hook_score', 0)):.2f}\n"
        f"views={int(row.get('view_count') or 0)}\n"
        f"{row.get('title', '')[:120]}\n"
        f"{row.get('url', '')}\n"
        f"#id {vid}\n"
        f"Нажми 👍 если ок, 👎 если нет"
    )


def _prune_bad_pending(*, limit: int = 120) -> int:
    """Drop obvious junk from queue — fast gates only (heavy checks run at send time)."""
    if limit <= 0:
        return 0
    from mlbb_youtube_shorts_ingest import (
        passes_mlbb_shorts_activity_gate,
        passes_mlbb_shorts_identity_gate,
    )

    deadline = time.time() + float(os.environ.get("MLBB_PRUNE_MAX_SEC", "60"))
    removed = 0
    for row in pending_candidates(limit=limit):
        if time.time() > deadline:
            print("prune_time_budget_exceeded", flush=True)
            break
        if row.get("ingest_verified"):
            continue
        path = Path(row.get("path", ""))
        vid = str(row.get("video_id", ""))
        if not path.exists() or not vid:
            continue
        title = str(row.get("title", ""))
        try:
            from mlbb_calibration_tier import prune_identity_enabled
        except ImportError:
            prune_identity_enabled = lambda: True  # noqa: E731
        checks = [passes_mlbb_shorts_activity_gate]
        if prune_identity_enabled():
            checks.insert(0, passes_mlbb_shorts_identity_gate)
        for check in checks:
            ok, reason = check(path, title=title)
            if not ok:
                reject_candidate(vid, reason=reason, path=path)
                removed += 1
                break
    if removed:
        print(f"pruned_bad_pending={removed}", flush=True)
    return removed


def _acquire_lock() -> object | None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text(encoding="utf-8").strip())
            os.kill(old_pid, 0)
        except ProcessLookupError:
            LOCK_PATH.unlink(missing_ok=True)
        except (ValueError, OSError):
            LOCK_PATH.unlink(missing_ok=True)
    handle = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        print("skip feed: another calibration_feed is running", flush=True)
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main() -> int:
    lock_handle = _acquire_lock()
    if lock_handle is None:
        return 0
    try:
        return _run_feed()
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_handle.close()


def _run_feed() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID") or env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1

    if os.environ.get("MLBB_FEED_REBUILD", "0") == "1":
        rebuild_index_from_disk()
    _prune_bad_pending(limit=int(os.environ.get("MLBB_PRUNE_PENDING_LIMIT", "10")))
    print(f"pick pending batch={BATCH_SIZE}", flush=True)
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
                "MLBB калибровка: очередь пуста — ingest ищет Shorts и MLBB-клипы (до 20 мин).\n"
                f"Индекс: {s['index_total']}, в очереди: {s['pending']}.\n"
                "Continuous worker качает и режет VOD параллельно.",
            )
            EMPTY_NOTIFY_PATH.parent.mkdir(parents=True, exist_ok=True)
            EMPTY_NOTIFY_PATH.write_text(json.dumps({"at": now}), encoding="utf-8")
        else:
            print(f"skip empty notify pending={s['pending']} quiet={QUIET_EMPTY_SEC}s")
        return 0

    s = stats()
    batch_header = (
        f"MLBB Shorts — {len(picked)} на оценку | 👍{s['feedback_yes']} 👎{s['feedback_no']}\n"
        "Под роликом — 👍 / 👎"
    )

    sent_ids: list[str] = []
    delivered = 0
    for idx, row in enumerate(picked, start=1):
        path = Path(row.get("path", ""))
        if not path.exists():
            continue
        vid = str(row.get("video_id", ""))
        title = str(row.get("title", ""))

        if is_stub_candidate(row):
            print(f"skip send {vid} stub=legacy_no_ingest", flush=True)
            reject_candidate(vid, reason="stub_no_ingest", path=path)
            continue

        from mlbb_youtube_shorts_ingest import resolve_shorts_send_path, verify_shorts_send_file

        print(f"check send {vid}", flush=True)
        cached_start = row.get("clip_start_sec")
        clip_start = float(cached_start) if cached_start is not None else None
        send_path, trim_start, open_reason = resolve_shorts_send_path(path, clip_start=clip_start)
        if send_path is None:
            print(f"skip send {vid} opening={open_reason}", flush=True)
            reject_candidate(vid, reason=open_reason, path=path)
            continue
        if trim_start > 0:
            print(f"trim send {vid} start={trim_start:.2f}s reason={open_reason}", flush=True)
            row = {**row, "trim_start_sec": trim_start}
        if row.get("ingest_verified"):
            final_ok, final_reason = True, "ingest_verified"
        else:
            final_ok, final_reason = verify_shorts_send_file(send_path, title=title)
        if not final_ok:
            print(f"skip send {vid} verify={final_reason}", flush=True)
            reject_candidate(vid, reason=final_reason, path=path)
            continue
        header = batch_header if delivered == 0 else ""
        caption = format_caption(row, idx, len(picked), header=header)
        if trim_start > 0:
            caption = f"{caption}\n✂️ trimmed {trim_start:.1f}s junk head"
        ok = send_video(token, chat_id, send_path, caption, video_id=vid)
        if not ok:
            ok = send_message(
                token,
                chat_id,
                f"#{idx} (ссылка, видео не загрузилось)\n{caption}",
                video_id=vid,
            )
        if ok:
            sent_ids.append(vid)
            mark_feed_sent([vid], paths=[path])
            delivered += 1
        else:
            print(f"delivery failed video_id={vid}", flush=True)
        time.sleep(0.4)

    print(f"sent={delivered}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
