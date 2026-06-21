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
    backfill_gameplay_flags,
    claim_feed_candidates,
    feed_singleton_lock,
    inline_keyboard_markup,
    mark_feed_blocked,
    mark_feed_sent,
    pending_candidates,
    rebuild_index_from_disk,
    refill_pending_emergency,
    release_feed_claims,
    release_stale_claims,
    repair_index,
    rescore_pending_candidates,
    stats,
    row_corresponds_to_mlbb,
)
from gameplay_gate import is_mlbb_calibration_short
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
BATCH_SIZE = int(os.environ.get("MLBB_CALIBRATION_BATCH", "3"))
from mlbb_telegram_video import (
    send_calibration_video,
)
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
    from mlbb_learning_first import can_send, record_send

    ok_send, reason = can_send(1)
    if not ok_send:
        print(f"send blocked video_id={video_id} reason={reason}")
        return False
    markup = inline_keyboard_markup(video_id) if video_id else None
    ok = send_calibration_video(token, chat_id, path, caption, reply_markup=markup)
    if ok:
        record_send(1)
    else:
        print(f"send blocked video_id={video_id} reason=telegram_api")
    return ok


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
    gscore = row.get("gameplay_score")
    hud = f" | hud={float(gscore):.3f}" if gscore is not None else ""
    oscore = row.get("owner_score")
    learn = f" | learn={float(oscore):.3f}" if oscore is not None else ""
    return (
        f"MLBB калибровка {idx}/{total}\n"
        f"score={float(row.get('score', 0)):.3f}{hud}{learn} | hook={float(row.get('hook_score', 0)):.2f}\n"
        f"views={int(row.get('view_count') or 0)}\n"
        f"{row.get('title', '')[:120]}\n"
        f"{row.get('url', '')}\n"
        f"#id {vid}\n"
        f"Нажми 👍 или 👎 под видео"
    )


def main() -> int:
    with feed_singleton_lock() as acquired:
        if not acquired:
            print("skip feed another instance running")
            return 0
        return _run_feed()


def _pick_unique_batch(rows: list[dict], *, batch_size: int | None = None) -> list[dict]:
    limit = batch_size if batch_size is not None else BATCH_SIZE
    unique: list[dict] = []
    seen_paths: set[str] = set()
    seen_vids: set[str] = set()
    for row in rows:
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
        if len(unique) >= limit:
            break
    return unique


def _run_feed() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    batch_size = int(env.get("MLBB_CALIBRATION_BATCH", os.environ.get("MLBB_CALIBRATION_BATCH", "3")))
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID") or os.environ.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG_BOT_TOKEN or TG_CHAT_ID missing", file=sys.stderr)
        return 1

    if env.get("MLBB_FEED_SKIP_REBUILD", "0") != "1":
        repair_index()
        rebuild_index_from_disk()
    load_max = float(env.get("MLBB_RESCORE_LOAD_MAX", "18"))
    if (
        env.get("MLBB_FEED_RESCORE", "1") == "1"
        and os.getloadavg()[0] < load_max
    ):
        rescore_pending_candidates(
            limit=int(env.get("MLBB_RESCORE_LIMIT", os.environ.get("MLBB_RESCORE_LIMIT", "8")))
        )
    stale = release_stale_claims(
        max_age_sec=float(env.get("MLBB_CLAIM_STALE_SEC", "300"))
    )
    if stale:
        print(f"released_stale_claims={stale}")
    backfill_limit = int(env.get("MLBB_FEED_BACKFILL_LIMIT", "0"))
    if backfill_limit > 0:
        backfill_gameplay_flags(limit=backfill_limit)
    picked = pending_candidates(limit=max(batch_size * 3, 12), repair=False)
    if not picked:
        rebuild_index_from_disk()
        picked = pending_candidates(limit=max(batch_size * 3, 12), repair=False)
    picked = claim_feed_candidates(_pick_unique_batch(picked, batch_size=batch_size))
    if not picked:
        refilled = refill_pending_emergency(limit=int(env.get("MLBB_REFILL_LIMIT", "15")))
        if refilled:
            print(f"refill_pending={refilled}")
            picked = claim_feed_candidates(
                _pick_unique_batch(
                    pending_candidates(limit=max(batch_size * 3, 12), repair=False),
                    batch_size=batch_size,
                )
            )
    if not picked:
        s = stats()
        if (
            s["pending"] == 0
            and os.environ.get("MLBB_FEED_TRY_INGEST", "0") == "1"
        ):
            worker_ingest = subprocess.run(
                ["pgrep", "-f", "mlbb_youtube_shorts_ingest.py"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if worker_ingest:
                print(f"skip feed ingest worker_ingest={worker_ingest.strip().split()[0]}")
            else:
                burst = Path("/usr/local/bin/mlbb_shorts_burst_send.py")
                if not burst.exists():
                    burst = Path(__file__).resolve().parent / "mlbb_shorts_burst_send.py"
                if burst.exists():
                    subprocess.run(
                        [sys.executable, str(burst)],
                        env={**env, "MLBB_BURST_TARGET": env.get("MLBB_BURST_TARGET", "6")},
                        timeout=int(os.environ.get("MLBB_FEED_BURST_TIMEOUT_SEC", "120")),
                        check=False,
                    )
                rebuild_index_from_disk()
                picked = claim_feed_candidates(
                    _pick_unique_batch(
                        pending_candidates(limit=max(batch_size * 3, 12), repair=False),
                        batch_size=batch_size,
                    )
                )
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
                "MLBB калибровка: очередь пуста — ingest ищет свежие Shorts (2024+).\n"
                f"Индекс: {s['index_total']}, в очереди: {s['pending']}.\n"
                "Continuous worker качает новые Shorts (~15/час).",
            )
            EMPTY_NOTIFY_PATH.parent.mkdir(parents=True, exist_ok=True)
            EMPTY_NOTIFY_PATH.write_text(json.dumps({"at": now}), encoding="utf-8")
        else:
            print(f"skip empty notify pending={s['pending']} quiet={QUIET_EMPTY_SEC}s")
        return 0

    sent_ids: list[str] = []
    skipped_ids: list[str] = []
    failed_ids: list[str] = []
    for idx, row in enumerate(picked, start=1):
        path = Path(row.get("path", ""))
        if not path.exists():
            failed_ids.append(str(row.get("video_id", "")))
            continue
        vid = str(row.get("video_id", ""))
        from mlbb_telegram_video import probe_duration

        if probe_duration(path) < 3.0:
            print(f"skip corrupt video_id={vid}")
            failed_ids.append(vid)
            continue
        corr = row_corresponds_to_mlbb(row)
        if corr:
            print(f"skip no MLBB correspondence video_id={vid} reason={corr}")
            mark_feed_blocked(vid, reason=corr, score=0.0)
            skipped_ids.append(vid)
            continue
        if env.get("MLBB_FEED_RE_GATE", "1") == "1":
            ok_mlbb, gscore, gate_reason = is_mlbb_calibration_short(
                path, description=str(row.get("title", ""))
            )
            if not ok_mlbb:
                print(f"skip non-gameplay video_id={vid} reason={gate_reason} score={gscore:.3f}")
                mark_feed_blocked(vid, reason=gate_reason, score=gscore)
                skipped_ids.append(vid)
                continue
        caption = format_caption(row, idx, len(picked))
        ok = send_video(token, chat_id, path, caption, video_id=vid)
        if not ok:
            send_message(
                token,
                chat_id,
                f"#{idx} (не удалось отправить)\n{caption}",
                video_id=vid,
            )
            failed_ids.append(vid)
            continue
        sent_ids.append(vid)
        mark_feed_sent([vid], paths=[path])
        time.sleep(1.2)

    if sent_ids:
        send_message(
            token,
            chat_id,
            f"MLBB Shorts — отправлено {len(sent_ids)} из {len(picked)}.\n"
            f"Статистика: 👍{stats()['feedback_yes']} 👎{stats()['feedback_no']}",
        )

    if skipped_ids or failed_ids:
        release_feed_claims(skipped_ids + failed_ids)

    print(f"sent={len(sent_ids)} skipped={len(skipped_ids)} failed={len(failed_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
