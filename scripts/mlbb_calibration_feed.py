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
    _pending_excluded,
    inline_keyboard_markup,
    is_stub_candidate,
    labeled_ids,
    load_feed_sent,
    mark_feed_sent,
    mark_ingest_skip,
    pending_candidates,
    pending_send_context,
    rebuild_index_from_disk,
    reject_candidate,
    stats,
)
from youtube_download import load_env
from youtube_watch_link import youtube_watch_url_from_row as _watch_link

ENV_PATH = Path("/root/.video_bot.env")
BATCH_SIZE = int(os.environ.get("MLBB_CALIBRATION_BATCH", "6"))
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024
QUIET_EMPTY_SEC = int(os.environ.get("MLBB_FEED_QUIET_EMPTY_SEC", "7200"))  # 2h
EMPTY_NOTIFY_PATH = DATA_MLBB / "calibration_feed_empty_notify.json"
LOCK_PATH = DATA_MLBB / "calibration_feed.lock"
_OEMBED_CACHE: dict[str, dict] = {}


def _youtube_oembed(video_id: str) -> dict:
    vid = str(video_id).strip()
    if not vid or len(vid) != 11:
        return {}
    if vid in _OEMBED_CACHE:
        return _OEMBED_CACHE[vid]
    import urllib.request

    url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json"
    try:
        with urllib.request.urlopen(url, timeout=12) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        data = {}
    _OEMBED_CACHE[vid] = data
    return data


def _youtube_thumb_url(video_id: str) -> str:
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"


def enrich_row_metadata(row: dict) -> dict:
    """Fill missing YouTube title/author for rescued or stale index rows."""
    vid = str(row.get("video_id") or row.get("id") or "").strip()
    title = str(row.get("title") or "").strip()
    if not vid:
        return row
    out = dict(row)
    if not out.get("url"):
        out["url"] = f"https://youtu.be/{vid}"
    if title and title != vid and len(title) > 12:
        out["url"] = _watch_link(out)
        return out
    meta = _youtube_oembed(vid)
    if meta.get("title"):
        out["title"] = str(meta["title"])
    if meta.get("author_name") and not out.get("channel"):
        out["channel"] = str(meta["author_name"])
    out["url"] = _watch_link(out)
    return out


def _human_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} МБ"
    if n >= 1024:
        return f"{n / 1024:.0f} КБ"
    return f"{n} Б"


def _probe_duration_sec(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    try:
        return float((proc.stdout or "0").strip())
    except ValueError:
        return 0.0


def _video_fail_reason(path: Path, *, api_payload: dict | None = None) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "файл не найден"
    if size > TELEGRAM_MAX_BYTES:
        return f"файл {_human_bytes(size)} — лимит Telegram {_human_bytes(TELEGRAM_MAX_BYTES)}"
    if api_payload and not api_payload.get("ok"):
        desc = str(api_payload.get("description") or "")
        if desc:
            return desc[:160]
    return "ошибка загрузки в Telegram"


def _shrink_for_telegram(path: Path) -> Path | None:
    """Last resort: re-encode oversized mp4 to fit Telegram limit."""
    try:
        if path.stat().st_size <= TELEGRAM_MAX_BYTES:
            return path
    except OSError:
        return None
    out_dir = DATA_MLBB / "calibration_tg_shrink"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"tg_{path.stem}.mp4"
    if out.exists() and out.stat().st_size > 2048:
        return out if out.stat().st_size <= TELEGRAM_MAX_BYTES else None
    dur = _probe_duration_sec(path)
    scale = "scale='min(720,iw)':-2" if dur > 90 else "scale='min(1080,iw)':-2"
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vf",
        scale,
        "-c:v",
        "libx264",
        "-crf",
        os.environ.get("MLBB_TG_SHRINK_CRF", "30"),
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(out),
    ]
    try:
        subprocess.run(cmd, check=False, timeout=180, capture_output=True)
    except subprocess.TimeoutExpired:
        return None
    if out.exists() and out.stat().st_size <= TELEGRAM_MAX_BYTES:
        return out
    return None


def send_photo_preview(
    token: str,
    chat_id: str,
    video_id: str,
    caption: str,
) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    cmd = [
        "curl",
        "-sS",
        "-m",
        "60",
        "-F",
        f"chat_id={chat_id}",
        "-F",
        f"photo={_youtube_thumb_url(video_id)}",
        "-F",
        f"caption={caption[:900]}",
        "-F",
        f"reply_markup={json.dumps(inline_keyboard_markup(video_id), ensure_ascii=False)}",
        url,
    ]
    clean_env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    result = subprocess.run(cmd, capture_output=True, text=True, env=clean_env, timeout=70)
    try:
        return bool(json.loads(result.stdout or "{}").get("ok"))
    except json.JSONDecodeError:
        return False


def send_video(
    token: str,
    chat_id: str,
    path: Path,
    caption: str,
    *,
    video_id: str = "",
) -> tuple[bool, str, dict]:
    from mlbb_learning_first import can_send, record_send

    ok_send, reason = can_send(1)
    if not ok_send:
        print(f"send blocked video_id={video_id} reason={reason}")
        return False, reason, {}
    try:
        size = path.stat().st_size
    except OSError:
        return False, "файл не найден", {}
    if size > TELEGRAM_MAX_BYTES:
        return False, _video_fail_reason(path), {}
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
        return False, "invalid telegram response", {}
    ok = bool(payload.get("ok"))
    if ok:
        record_send(1)
        return True, "ok", payload
    return False, _video_fail_reason(path, api_payload=payload), payload


def send_video_best_effort(
    token: str,
    chat_id: str,
    path: Path,
    caption: str,
    *,
    video_id: str = "",
) -> tuple[bool, str]:
    ok, reason, _ = send_video(token, chat_id, path, caption, video_id=video_id)
    if ok:
        return True, "ok"
    shrunk = _shrink_for_telegram(path)
    if shrunk and shrunk != path:
        ok2, reason2, _ = send_video(token, chat_id, shrunk, caption, video_id=video_id)
        if ok2:
            return True, "shrunk"
        return False, reason2
    return False, reason


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


def format_caption(row: dict, idx: int, total: int, *, header: str = "", send_dur: float = 0.0) -> str:
    row = enrich_row_metadata(row)
    vid = row.get("video_id", "")
    prefix = f"{header}\n" if header else ""
    channel = str(row.get("channel") or "").strip()
    channel_line = f"канал: {channel}\n" if channel else ""
    watch = _watch_link(row)
    return (
        f"{prefix}MLBB калибровка {idx}/{total}\n"
        f"{watch}\n"
        f"score={float(row.get('score', 0)):.3f} | hook={float(row.get('hook_score', 0)):.2f}\n"
        f"views={int(row.get('view_count') or 0)}\n"
        f"{channel_line}"
        f"{row.get('title', '')[:120]}\n"
        f"#id {vid}\n"
        f"Нажми 👍 если ок, 👎 если нет"
    )


def format_link_fallback_caption(
    row: dict,
    idx: int,
    total: int,
    *,
    header: str,
    fail_reason: str,
    send_path: Path,
) -> str:
    row = enrich_row_metadata(row)
    vid = str(row.get("video_id", ""))
    send_dur = _probe_duration_sec(send_path)
    watch = _watch_link(row)
    try:
        size_line = f"файл на сервере: {_human_bytes(send_path.stat().st_size)}\n"
    except OSError:
        size_line = ""
    prefix = f"{header}\n" if header else ""
    return (
        f"{prefix}⚠️ Видео в Telegram не загрузилось\n"
        f"Причина: {fail_reason}\n\n"
        f"{watch}\n"
        f"{row.get('title', vid)[:140]}\n"
        f"{size_line}"
        f"score={float(row.get('score', 0)):.3f}\n"
        f"#id {vid} ({idx}/{total})\n"
        f"Нажми 👍 если момент ок, 👎 если нет"
    )


def _prune_bad_pending(*, limit: int = 120) -> int:
    """Drop obvious junk from queue — fast gates only (heavy checks run at send time)."""
    if limit <= 0:
        return 0
    from mlbb_youtube_shorts_ingest import (
        passes_mlbb_shorts_activity_gate,
        passes_mlbb_shorts_identity_gate,
    )

    batch = int(os.environ.get("MLBB_CALIBRATION_BATCH", "4"))
    queue = pending_candidates(limit=limit, repair=False)
    if len(queue) < batch:
        return 0  # never prune thin queue — would starve feed

    deadline = time.time() + float(os.environ.get("MLBB_PRUNE_MAX_SEC", "60"))
    removed = 0
    for row in queue:
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
        if prune_identity_enabled() and os.environ.get("MLBB_CALIBRATION_LENIENT", "0") != "1":
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
        from mlbb_calibration_store import index_unlabeled_disk_shorts, rebuild_index_from_disk

        rebuild_index_from_disk()
        indexed = index_unlabeled_disk_shorts(limit=int(os.environ.get("MLBB_DISK_INDEX_LIMIT", "24")))
        if indexed:
            print(f"feed_disk_index added={indexed}", flush=True)
    _prune_bad_pending(limit=int(os.environ.get("MLBB_PRUNE_PENDING_LIMIT", "10")))
    print(f"pick pending batch={BATCH_SIZE}", flush=True)

    def _pick_batch() -> list[dict]:
        picked = pending_candidates(limit=max(BATCH_SIZE * 3, 12))
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
            row = enrich_row_metadata(row)
            from mlbb_channel_blocklist import is_blocked_candidate

            blocked, block_reason = is_blocked_candidate(row)
            if blocked:
                reject_candidate(vid, reason=block_reason, path=path)
                print(f"skip pick {vid} {block_reason}", flush=True)
                continue
            seen_vids.add(vid)
            seen_paths.add(path_key)
            unique.append(row)
            if len(unique) >= BATCH_SIZE:
                break
        unique.sort(
            key=lambda r: (
                Path(str(r.get("path", ""))).stat().st_size
                if Path(str(r.get("path", ""))).exists()
                else 10**12
            )
        )
        return unique

    picked = _pick_batch()
    if not picked:
        from mlbb_calibration_store import index_unlabeled_disk_shorts, rebuild_index_from_disk

        rebuild_index_from_disk()
        added = index_unlabeled_disk_shorts(limit=int(os.environ.get("MLBB_DISK_INDEX_LIMIT", "24")))
        if added:
            print(f"feed_retry_disk_index added={added}", flush=True)
        picked = _pick_batch()

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
                "MLBB калибровка: очередь пуста — ingest ищет новые Shorts (без повторов).\n"
                f"Индекс: {s['index_total']}, в очереди: {s['pending']}.\n"
                "Уже отправленные без 👍/👎 не шлём повторно — ждём новые загрузки.",
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
    skipped_unsendable = 0
    labeled_ctx, sent_ctx, queue_starved = pending_send_context()
    for idx, row in enumerate(picked, start=1):
        path = Path(row.get("path", ""))
        if not path.exists():
            continue
        vid = str(row.get("video_id", ""))
        row = enrich_row_metadata(row)
        title = str(row.get("title", ""))

        from mlbb_channel_blocklist import is_blocked_candidate

        blocked, block_reason = is_blocked_candidate(row)
        if blocked:
            print(f"skip send {vid} {block_reason}", flush=True)
            reject_candidate(vid, reason=block_reason, path=path)
            continue

        if is_stub_candidate(row):
            print(f"skip send {vid} stub=legacy_no_ingest", flush=True)
            reject_candidate(vid, reason="stub_no_ingest", path=path)
            continue

        from mlbb_youtube_shorts_ingest import (
            passes_shorts_late_action_gate,
            resolve_shorts_send_path,
            verify_shorts_send_file,
        )

        labeled = labeled_ctx
        sent = sent_ctx
        if _pending_excluded(vid, path, labeled, sent, queue_starved=queue_starved):
            print(f"skip send {vid} already_sent_unlabeled", flush=True)
            skipped_unsendable += 1
            mark_ingest_skip(vid, "already_sent_unlabeled")
            continue

        print(f"check send {vid}", flush=True)
        cached_start = row.get("clip_start_sec")
        clip_start = float(cached_start) if cached_start is not None else None
        try:
            send_path, trim_start, open_reason = resolve_shorts_send_path(path, clip_start=clip_start)
        except subprocess.TimeoutExpired:
            print(f"skip send {vid} trim_timeout", flush=True)
            reject_candidate(vid, reason="trim_timeout", path=path)
            continue
        if send_path is None:
            print(f"skip send {vid} opening={open_reason}", flush=True)
            reject_candidate(vid, reason=open_reason, path=path)
            continue
        if trim_start > 0:
            print(f"trim send {vid} start={trim_start:.2f}s reason={open_reason}", flush=True)
            row = {**row, "trim_start_sec": trim_start, "send_reason": open_reason}
        elif open_reason:
            row = {**row, "send_reason": open_reason}
        if row.get("clip_start_sec") is None and trim_start > 0:
            row = {**row, "clip_start_sec": trim_start}
        if row.get("ingest_verified"):
            final_ok, final_reason = True, "ingest_verified"
            late_ok, late_reason = passes_shorts_late_action_gate(path, title=title)
            if not late_ok:
                final_ok, final_reason = False, late_reason
        else:
            final_ok, final_reason = verify_shorts_send_file(send_path, title=title)
        if not final_ok:
            print(f"skip send {vid} verify={final_reason}", flush=True)
            reject_candidate(vid, reason=final_reason, path=path)
            continue
        header = batch_header if delivered == 0 else ""
        send_dur = _probe_duration_sec(send_path)
        caption = format_caption(row, idx, len(picked), header=header, send_dur=send_dur)
        ok, fail_reason = send_video_best_effort(token, chat_id, send_path, caption, video_id=vid)
        if not ok:
            fallback = format_link_fallback_caption(
                row,
                idx,
                len(picked),
                header=header,
                fail_reason=fail_reason,
                send_path=send_path,
            )
            ok = send_photo_preview(token, chat_id, vid, fallback)
            if not ok:
                ok = send_message(token, chat_id, fallback, video_id=vid)
        if ok:
            sent_ids.append(vid)
            mark_feed_sent([vid], paths=[path])
            delivered += 1
        else:
            print(f"delivery failed video_id={vid}", flush=True)
        time.sleep(0.4)

    print(f"sent={delivered} skipped_unsendable={skipped_unsendable}")
    try:
        from mlbb_pipeline_health import record_feed_delivery

        record_feed_delivery(delivered=delivered, skipped_unsendable=skipped_unsendable)
    except ImportError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
