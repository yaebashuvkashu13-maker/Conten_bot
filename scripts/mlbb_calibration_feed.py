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
    mark_feed_sent,
    pending_candidates,
    rebuild_index_from_disk,
    reject_candidate,
    repair_index,
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
        f"Нажми 👍 или 👎 под видео"
    )


def _prune_bad_pending(*, limit: int = 120) -> int:
    """Drop queued static slides / wrong-game before picking a batch."""
    from mlbb_youtube_shorts_ingest import (
        passes_mlbb_shorts_activity_gate,
        passes_mlbb_shorts_gameplay_gate,
        passes_mlbb_shorts_identity_gate,
        passes_mlbb_shorts_verify_gate,
    )

    removed = 0
    for row in pending_candidates(limit=limit):
        path = Path(row.get("path", ""))
        vid = str(row.get("video_id", ""))
        if not path.exists() or not vid:
            continue
        title = str(row.get("title", ""))
        for check in (
            passes_mlbb_shorts_identity_gate,
            passes_mlbb_shorts_activity_gate,
            passes_mlbb_shorts_gameplay_gate,
            passes_mlbb_shorts_verify_gate,
        ):
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

    repair_index()
    rebuild_index_from_disk()
    _prune_bad_pending(limit=120)
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

    s = stats()
    batch_header = (
        f"MLBB Shorts — {len(picked)} на оценку | 👍{s['feedback_yes']} 👎{s['feedback_no']}\n"
        "Под роликом — кнопки 👍 / 👎"
    )

    sent_ids: list[str] = []
    delivered = 0
    for idx, row in enumerate(picked, start=1):
        path = Path(row.get("path", ""))
        if not path.exists():
            continue
        vid = str(row.get("video_id", ""))
        min_send_score = float(os.environ.get("MLBB_CALIBRATION_MIN_SEND_SCORE", "0.05"))
        lenient = os.environ.get("MLBB_CALIBRATION_LENIENT", "1") == "1"
        score = float(row.get("score") or 0)

        from mlbb_youtube_shorts_ingest import (
            passes_mlbb_shorts_activity_gate,
            passes_mlbb_shorts_gameplay_gate,
            passes_mlbb_shorts_identity_gate,
            passes_mlbb_shorts_verify_gate,
            passes_shorts_calibration_gate,
        )

        id_ok, id_reason = passes_mlbb_shorts_identity_gate(
            path, title=str(row.get("title", ""))
        )
        if not id_ok:
            print(f"skip send {vid} identity={id_reason}", flush=True)
            reject_candidate(vid, reason=id_reason, path=path)
            continue

        act_ok, act_reason = passes_mlbb_shorts_activity_gate(
            path, title=str(row.get("title", ""))
        )
        if not act_ok:
            print(f"skip send {vid} activity={act_reason}", flush=True)
            reject_candidate(vid, reason=act_reason, path=path)
            continue

        gp_ok, gp_reason = passes_mlbb_shorts_gameplay_gate(
            path, title=str(row.get("title", ""))
        )
        if not gp_ok:
            print(f"skip send {vid} gameplay={gp_reason}", flush=True)
            reject_candidate(vid, reason=gp_reason, path=path)
            continue

        ver_ok, ver_reason = passes_mlbb_shorts_verify_gate(
            path, title=str(row.get("title", ""))
        )
        if not ver_ok:
            print(f"skip send {vid} verify={ver_reason}", flush=True)
            reject_candidate(vid, reason=ver_reason, path=path)
            continue

        if score < min_send_score and not lenient:
            from mlbb_youtube_shorts_ingest import score_clip

            feats = score_clip(path)
            score = float(feats.get("score") or 0)
            row = {**row, **feats}
        if score < min_send_score and not lenient:
            print(f"skip send {vid} low_score={score}", flush=True)
            continue

        if lenient:
            gate_ok, gate_reason = True, "lenient"
        else:
            gate_ok, gate_reason = passes_shorts_calibration_gate(
                path, title=str(row.get("title", ""))
            )
            if not gate_ok:
                print(f"skip send {vid} gate={gate_reason}", flush=True)
                reject_candidate(vid, reason=gate_reason, path=path)
                continue
        header = batch_header if delivered == 0 else ""
        caption = format_caption(row, idx, len(picked), header=header)
        ok = send_video(token, chat_id, path, caption, video_id=vid)
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
