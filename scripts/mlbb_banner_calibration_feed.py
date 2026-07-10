#!/usr/bin/env python3
"""Send MLBB kill-banner screenshots to owner for button calibration (~50 checks)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_banner_calibration_capture import discover_candidates, render_check_screenshot
from mlbb_banner_calibration_reasons import inline_keyboard_markup
from mlbb_banner_calibration_store import (
    calibration_target,
    check_id,
    labeled_ids,
    load_sent,
    mark_sent,
    stats,
    upsert_check,
)
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
LOCK_PATH = Path(os.environ.get("MLBB_BANNER_CALIB_LOCK", "/tmp/mlbb_banner_calibration_feed.lock"))
BATCH_SIZE = int(os.environ.get("MLBB_BANNER_CALIB_BATCH", "3"))
VODS_PER_RUN = int(os.environ.get("MLBB_BANNER_CALIB_VODS", "2"))


@contextmanager
def feed_singleton_lock():
    acquired = False
    try:
        if LOCK_PATH.exists():
            age = time.time() - LOCK_PATH.stat().st_mtime
            if age < float(os.environ.get("MLBB_BANNER_CALIB_LOCK_MAX_SEC", "1800")):
                yield False
                return
            LOCK_PATH.unlink(missing_ok=True)
        LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
        acquired = True
        yield True
    finally:
        if acquired:
            LOCK_PATH.unlink(missing_ok=True)


def _inbox_root() -> Path:
    return Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))


def _pick_vods(inbox: Path, limit: int) -> list[Path]:
    files = [p for p in inbox.glob("yt_*.mp4") if p.is_file() and p.stat().st_size > 500_000]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def send_photo(
    token: str,
    chat_id: str,
    path: Path,
    caption: str,
    *,
    reply_markup: dict | None = None,
) -> bool:
    from mlbb_telegram_video import send_photo_file

    return send_photo_file(token, chat_id, path, caption, reply_markup=reply_markup)


def send_message(token: str, chat_id: str, text: str) -> None:
    cmd = [
        "curl",
        "-sS",
        "-F",
        f"chat_id={chat_id}",
        "-F",
        f"text={text[:3900]}",
        f"https://api.telegram.org/bot{token}/sendMessage",
    ]
    subprocess.run(
        cmd,
        env={k: v for k, v in os.environ.items() if "proxy" not in k.lower()},
        check=False,
        timeout=30,
    )


def format_caption(row: dict, idx: int, total: int) -> str:
    st = stats()
    return (
        f"🎯 Банер-калибровка {idx}/{total} | всего {st['labeled']}/{st['target']}\n"
        f"VOD {row.get('vod_id', '')} @ {float(row.get('sec', 0)):.1f}s\n"
        f"бот: tier={row.get('banner_tier')} {row.get('banner_label', '')} "
        f"({row.get('banner_source', '')})\n"
        f"{str(row.get('detected_text', ''))[:100]}\n"
        f"#{row.get('check_id', '')}\n"
        f"Зелёная рамка = зона банера. Нажми причину."
    )


def _register_candidates(vod: Path) -> int:
    from mlbb_banner_calibration_positive_feed import _read_frame, verified_before_send

    labeled = labeled_ids()
    sent = load_sent()
    added = 0
    skipped = 0
    from mlbb_vod_dense_hints import audit_banner_hints

    extra_secs: list[float] = []
    if os.environ.get("MLBB_BANNER_CALIB_USE_SEGMENT_INDEX", "1") == "1":
        try:
            from mlbb_vod_segment_store import load_index

            vid = vod.stem.replace("yt_", "")[:11]
            seg_limit = int(os.environ.get("MLBB_BANNER_CALIB_SEGMENT_HINTS", "6"))
            for row in load_index().get("segments", []):
                seg_vod = str(row.get("vod_id") or row.get("vod") or "")
                if vid not in seg_vod:
                    continue
                if row.get("kill_banner") in (None, ""):
                    continue
                peak = row.get("peak_start", row.get("start"))
                if peak is not None:
                    extra_secs.append(float(peak))
                if len(extra_secs) >= seg_limit:
                    break
        except Exception:
            pass
    for sec in audit_banner_hints(vod.stem.replace("yt_", "")[:11], min_tier=1):
        extra_secs.append(sec)

    hits = discover_candidates(vod)
    if extra_secs:
        from mlbb_kill_banner import KillBannerHit, find_banner_near_peak

        seen = {round(h.sec, 1) for h in hits}
        for sec in sorted(set(extra_secs)):
            if any(abs(sec - s) < 5 for s in seen):
                continue
            hit = find_banner_near_peak(vod, sec, quick=True)
            if hit is None:
                hit = KillBannerHit(sec=round(sec, 2), tier=3, label="segment", text="index", source="index")
            hits.append(hit)
            seen.add(round(sec, 1))

    hits = sorted(hits, key=lambda h: (-int(h.tier), h.sec))
    for hit in hits:
        cid = check_id(vod, hit.sec)
        if cid in labeled or cid in sent:
            continue
        frame = _read_frame(vod, hit.sec)
        ok, why = verified_before_send(vod, hit, frame)
        if not ok:
            skipped += 1
            print(f"skip_register {cid}: {why}")
            continue
        try:
            render_check_screenshot(vod, hit.sec, hit=hit)
            added += 1
        except Exception as exc:
            print(f"capture failed {vod.name}@{hit.sec}: {exc}")
    if skipped:
        print(json.dumps({"skipped_unverified": skipped}, ensure_ascii=False))
    return added


def _send_batch(token: str, chat_id: str) -> int:
    from mlbb_banner_calibration_positive_feed import _read_frame, hit_from_check_row, verified_before_send
    from mlbb_banner_calibration_store import pending_for_send, remove_check_from_index

    pending = pending_for_send(limit=BATCH_SIZE)
    if not pending:
        return 0
    sent_n = 0
    for i, row in enumerate(pending, start=1):
        cid = str(row.get("check_id", ""))
        shot = Path(str(row.get("screenshot", "")))
        if not shot.exists():
            continue
        vod = Path(str(row.get("vod", "")))
        if vod.exists():
            hit = hit_from_check_row(row)
            frame = _read_frame(vod, hit.sec)
            ok, why = verified_before_send(vod, hit, frame)
            if not ok:
                print(f"skip_send {cid}: {why}")
                remove_check_from_index(cid)
                continue
        markup = inline_keyboard_markup(cid)
        caption = format_caption(row, i, len(pending))
        if send_photo(token, chat_id, shot, caption, reply_markup=markup):
            mark_sent([cid])
            sent_n += 1
            print(f"sent banner_cal {cid}")
        else:
            print(f"send failed banner_cal {cid}")
    return sent_n


def main() -> int:
    with feed_singleton_lock() as acquired:
        if not acquired:
            print("skip banner_cal feed: another instance running")
            return 0
        return _run_feed()


def _run_feed() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("missing TG_BOT_TOKEN or TG_CHAT_ID")
        return 1

    st = stats()
    force = os.environ.get("MLBB_BANNER_CALIB_FORCE", "0") == "1"
    if not force and st["labeled"] >= calibration_target():
        print(json.dumps({"status": "target_reached", **st}, ensure_ascii=False))
        return 0
    no_banner = int((st.get("by_reason") or {}).get("no_banner", 0))
    neg_stop = int(os.environ.get("MLBB_BANNER_CALIB_NEG_STOP", "50"))
    if not force and no_banner >= neg_stop:
        print(
            json.dumps(
                {"status": "neg_bank_sufficient", "no_banner": no_banner, "note": "use positive scan only", **st},
                ensure_ascii=False,
            )
        )
        return 0

    inbox = _inbox_root()
    if not inbox.exists():
        print(f"inbox missing: {inbox}")
        return 1

    registered = 0
    for vod in _pick_vods(inbox, VODS_PER_RUN):
        registered += _register_candidates(vod)

    sent = _send_batch(token, chat_id)
    st = stats()
    print(json.dumps({"registered": registered, "sent": sent, **st}, ensure_ascii=False))

    if sent == 0 and st["remaining_to_target"] > 0 and registered == 0:
        send_message(
            token,
            chat_id,
            f"⚠️ Банер-калибровка: нет новых кандидатов ({st['labeled']}/{st['target']}). "
            f"Положи свежие VOD в inbox или снизь MLBB_BANNER_CALIB_MIN_TIER.",
        )
    elif sent > 0:
        send_message(
            token,
            chat_id,
            f"📸 Банер-калибровка: отправил {sent} скрин(ов). "
            f"Размечено {st['labeled']}/{st['target']}.",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
