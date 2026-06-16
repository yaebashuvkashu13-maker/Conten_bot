#!/usr/bin/env python3
"""
MLBB Telegram callback handlers — 👍/👎 for Shorts and VOD segments.

Import from telegram_upload_bot.py:
    from mlbb_telegram_handlers import handle_callback_query
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_telegram_send import bot_token, is_owner, load_env, owner_chat_id, send_message

log = logging.getLogger("mlbb_telegram_handlers")


def _api_call(method: str, payload: dict | None = None, *, timeout: int = 60) -> dict:
    token = bot_token()
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode())
    if not result.get("ok"):
        desc = str(result.get("description", ""))
        # Harmless when markup already updated or message too old to edit.
        if method == "editMessageReplyMarkup" and any(
            x in desc.lower() for x in ("not modified", "message is not modified", "message to edit not found")
        ):
            return {}
        raise RuntimeError(f"Telegram API error for {method}: {result}")
    return result["result"]


def schedule_mlbb_retrain(*, force: bool = False) -> None:
    """Debounced retrain — every N labels or MLBB_RETRAIN_MIN_HOURS, not every 👍."""
    try:
        from mlbb_learning_first import record_label_for_retrain, should_run_retrain

        record_label_for_retrain()
        ok, reason = should_run_retrain(force=force)
        if not ok:
            log.debug("mlbb retrain deferred: %s", reason)
            return
        log.info("mlbb retrain scheduled: %s", reason)
    except ImportError:
        pass

    for script in (
        Path("/usr/local/bin/mlbb_learn_apply.sh"),
        Path(__file__).resolve().parent / "mlbb_learn_apply.sh",
    ):
        if not script.exists():
            continue
        try:
            subprocess.Popen(
                ["bash", str(script)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            log.exception("mlbb retrain schedule failed")
        return


def apply_vseg_label(
    chat_id: str | int,
    segment_id: str,
    *,
    is_good: bool,
    reason: str = "",
) -> tuple[bool, str]:
    from mlbb_vod_segment_store import apply_owner_label, find_segment, stats

    sid = segment_id.strip()
    ok, _label = apply_owner_label(sid, is_good=is_good, reason=reason, by_chat=str(chat_id))
    s = stats()
    if not ok:
        return False, f"Не нашёл кусок {sid}. Запусти /mlbb_vod"
    schedule_mlbb_retrain()
    if is_good:
        return True, f"✅ Ок — кусок {sid}\nВсего VOD: 👍{s['feedback_yes']} 👎{s['feedback_no']}"

    row = find_segment(sid) or {}
    peak = float(row.get("peak_start") or row.get("start") or 0)
    vid = str(row.get("vod_id") or sid.rsplit("_", 1)[0])
    owner_report = ""
    try:
        from mlbb_learning_first import dislike_feedback_report

        owner_report = dislike_feedback_report(sid, vod_id=vid, peak_sec=peak, reason=reason)
        if owner_report:
            send_message(owner_report)
    except ImportError:
        pass
    return True, (
        f"❌ Не ок — кусок {sid}\n"
        f"Причина: {reason or '—'}\n"
        f"Всего VOD: 👍{s['feedback_yes']} 👎{s['feedback_no']}"
        + (f"\n\n{owner_report}" if owner_report else "")
    )


def apply_shorts_label(
    chat_id: str | int,
    video_id: str,
    *,
    is_good: bool,
    reason: str = "",
) -> tuple[bool, str]:
    from mlbb_calibration_store import apply_owner_label, stats

    vid = video_id.strip()
    ok, _label = apply_owner_label(vid, is_good=is_good, reason=reason, by_chat=str(chat_id))
    s = stats()
    if not ok:
        if str(_label).startswith("file_missing"):
            return False, f"Файл #{vid} уже удалён с сервера."
        return False, f"Не нашёл #{vid}. Возможно, это старое сообщение — дождись новой партии от бота."
    schedule_mlbb_retrain()
    if is_good:
        return (
            True,
            f"✅ Записал good exemplar #{vid}\n"
            f"Всего: 👍{s['feedback_yes']} 👎{s['feedback_no']} | accuracy {s.get('accuracy', 0):.0%}",
        )
    return (
        True,
        f"❌ Записал bad exemplar #{vid}\n"
        f"Причина: {reason or '—'}\n"
        f"Всего: 👍{s['feedback_yes']} 👎{s['feedback_no']} | accuracy {s.get('accuracy', 0):.0%}",
    )


def parse_callback_data(data: str) -> tuple[str, bool | None, str, str]:
    """Return (mode, is_good, item_id, reason). mode: shorts|vseg|hq_shorts|hq_vseg|noop|unknown."""
    if data == "mlbb_noop":
        return "noop", None, "", ""
    if data.startswith("mlbb_yes:"):
        return "shorts", True, data.split(":", 1)[1].strip(), ""
    if data.startswith("mlbb_no:"):
        return "shorts", False, data.split(":", 1)[1].strip(), "button_dislike"
    if data.startswith("mlbb_vseg_yes:"):
        return "vseg", True, data.split(":", 1)[1].strip(), ""
    if data.startswith("mlbb_vseg_no:"):
        return "vseg", False, data.split(":", 1)[1].strip(), "button_dislike"
    if data.startswith("mlbb_hq_shorts:"):
        return "hq_shorts", None, data.split(":", 1)[1].strip(), ""
    if data.startswith("mlbb_hq_vseg:"):
        return "hq_vseg", None, data.split(":", 1)[1].strip(), ""
    return "unknown", None, "", ""


def _handle_download_original(
    *,
    chat_id: str | int,
    message_id: int,
    query_id: str,
    mode: str,
    item_id: str,
    api,
) -> None:
    """Send HQ file on button press (👍 label already recorded)."""

    def _worker() -> None:
        try:
            if mode == "hq_vseg":
                ok, reply = send_vseg_hq(chat_id, item_id)
                if not ok:
                    send_message(f"⚠️ {reply}", chat_id=str(chat_id))
                    return
                from mlbb_vod_segment_store import labeled_keyboard_markup as markup_fn

                markup = markup_fn("good", segment_id=item_id)
            else:
                ok, reply = send_shorts_hq(chat_id, item_id)
                if not ok:
                    send_message(f"⚠️ {reply}", chat_id=str(chat_id))
                    return
                from mlbb_calibration_store import labeled_keyboard_markup as markup_fn

                markup = markup_fn("good", video_id=item_id)
            api(
                "editMessageReplyMarkup",
                {"chat_id": chat_id, "message_id": message_id, "reply_markup": markup},
                timeout=15,
            )
        except Exception:
            log.exception("download original failed mode=%s id=%s", mode, item_id)

    try:
        api(
            "answerCallbackQuery",
            {"callback_query_id": query_id, "text": "Отправляю оригинал…"},
            timeout=15,
        )
    except Exception:
        pass
    threading.Thread(
        target=_worker,
        daemon=True,
        name=f"mlbb-dl-{mode}-{item_id[:12]}",
    ).start()


def _ffprobe_duration(path: Path) -> float:
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
        timeout=30,
    )
    try:
        return float((proc.stdout or "0").strip())
    except ValueError:
        return 0.0


def _segment_duration(row: dict) -> float:
    path = Path(str(row.get("path", "")))
    if path.exists():
        dur = _ffprobe_duration(path)
        if dur > 0:
            return dur
    for key in ("duration", "input_duration", "output_duration"):
        if row.get(key):
            return float(row[key])
    return float(os.environ.get("MLBB_CALIBRATION_CLIP_SEC", "30"))


def _cut_vod_hq(vod: Path, start: float, dur: float, out: Path) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    base = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{dur:.3f}",
        "-i",
        str(vod),
        "-movflags",
        "+faststart",
    ]
    for cmd in (
        [*base, "-c", "copy", str(out)],
        [
            *base,
            "-c:v",
            "libx264",
            "-crf",
            os.environ.get("MLBB_HQ_CRF", "17"),
            "-preset",
            os.environ.get("MLBB_HQ_PRESET", "fast"),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(out),
        ],
    ):
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
        if out.exists() and out.stat().st_size > 2048:
            return True
        log.warning("hq cut failed vod=%s start=%.1f cmd=%s err=%s", vod.name, start, cmd[-3], proc.stderr[:200])
    return False


def resolve_vseg_hq_path(segment_id: str) -> tuple[Path | None, bool]:
    """Return HQ path; second value True when caller should delete the temp file."""
    from mlbb_vod_segment_store import find_segment

    row = find_segment(segment_id.strip()) or {}
    vod = Path(str(row.get("vod", "")))
    start = float(row.get("start", 0))
    dur = _segment_duration(row)
    if vod.exists() and os.environ.get("MLBB_HQ_SOURCE_VOD", "1") == "1":
        out = Path(tempfile.gettempdir()) / f"hq_{segment_id.strip()}.mp4"
        if _cut_vod_hq(vod, start, dur, out):
            return out, True
    path = Path(str(row.get("path", "")))
    if path.exists():
        return path, False
    return None, False


def send_hq_document(chat_id: str | int, path: Path, *, caption: str = "") -> bool:
    """Send original mp4 as file (Telegram preserves quality up to 2GB)."""
    if not path.exists() or path.stat().st_size < 2048:
        return False
    token = bot_token()
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    cmd = [
        "curl",
        "-sS",
        "--noproxy",
        "*",
        "-m",
        "600",
        "-F",
        f"chat_id={chat_id}",
        "-F",
        f"document=@{path}",
    ]
    if caption:
        cmd.extend(["-F", f"caption={caption[:900]}"])
    cmd.append(url)
    clean_env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    result = subprocess.run(cmd, capture_output=True, text=True, env=clean_env, timeout=610)
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        log.error("hq send invalid json stdout=%r stderr=%r", result.stdout[:300], result.stderr[:300])
        return False
    if not payload.get("ok"):
        log.error("hq send failed path=%s size=%s resp=%s", path, path.stat().st_size, payload)
        return False
    return True


def send_shorts_hq(chat_id: str | int, video_id: str) -> tuple[bool, str]:
    from mlbb_calibration_store import SHORTS_ROOT, find_candidate_or_labeled

    vid = video_id.strip()
    row = find_candidate_or_labeled(vid) or {}
    path = Path(str(row.get("path", "")))
    if not path.exists():
        path = SHORTS_ROOT / f"yt_{vid}.mp4"
    if not path.exists():
        return False, f"Файл #{vid} не найден на сервере."
    title = str(row.get("title") or vid)
    ok = send_hq_document(chat_id, path, caption=f"HQ #{vid}\n{title[:120]}")
    return (True, "Отправил HQ-файл") if ok else (False, "Не удалось отправить HQ (лимит Telegram?)")


def send_vseg_hq(chat_id: str | int, segment_id: str) -> tuple[bool, str]:
    sid = segment_id.strip()
    path, is_temp = resolve_vseg_hq_path(sid)
    if not path:
        return False, f"Кусок {sid} не найден."
    try:
        ok = send_hq_document(chat_id, path, caption=f"HQ segment {sid}")
    finally:
        if is_temp:
            path.unlink(missing_ok=True)
    return (True, "Отправил HQ-файл") if ok else (False, "Не удалось отправить HQ (лимит Telegram?)")


def handle_callback_query(query: dict, *, api=_api_call) -> None:
    """Process Telegram callback_query for MLBB 👍/👎 buttons."""
    query_id = query.get("id")
    data = str(query.get("data") or "")
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if not query_id or chat_id is None or message_id is None:
        return

    env = load_env()
    if not is_owner(chat_id, env):
        try:
            api(
                "answerCallbackQuery",
                {"callback_query_id": query_id, "text": "Нет доступа", "show_alert": True},
                timeout=15,
            )
        except Exception:
            pass
        return

    mode, is_good, item_id, reason = parse_callback_data(data)
    if mode == "noop":
        try:
            api("answerCallbackQuery", {"callback_query_id": query_id}, timeout=15)
        except Exception:
            pass
        return
    if mode in ("hq_shorts", "hq_vseg"):
        try:
            _handle_download_original(
                chat_id=chat_id,
                message_id=message_id,
                query_id=query_id,
                mode=mode,
                item_id=item_id,
                api=api,
            )
        except Exception as exc:
            log.exception("hq send failed: %s", exc)
            api(
                "answerCallbackQuery",
                {"callback_query_id": query_id, "text": str(exc)[:180], "show_alert": True},
                timeout=15,
            )
        return
    if mode == "unknown" or is_good is None:
        try:
            api("answerCallbackQuery", {"callback_query_id": query_id}, timeout=15)
        except Exception:
            pass
        return

    try:
        # Telegram drops callbacks after ~30s — ack immediately, then save label.
        api(
            "answerCallbackQuery",
            {
                "callback_query_id": query_id,
                "text": "Принято…" if is_good else "Записал 👎",
            },
            timeout=10,
        )

        if mode == "vseg":
            ok, reply = apply_vseg_label(chat_id, item_id, is_good=is_good, reason=reason)
            if is_good:
                from mlbb_vod_segment_store import good_download_keyboard_markup as markup_fn

                markup = markup_fn(item_id)
            else:
                from mlbb_vod_segment_store import labeled_keyboard_markup as markup_fn

                markup = markup_fn("bad", segment_id=item_id)
        else:
            ok, reply = apply_shorts_label(chat_id, item_id, is_good=is_good, reason=reason)
            if not ok:
                send_message(reply, chat_id=str(chat_id))
                return
            if is_good:
                from mlbb_calibration_store import good_download_keyboard_markup as markup_fn

                markup = markup_fn(item_id)
            else:
                from mlbb_calibration_store import labeled_keyboard_markup as markup_fn

                markup = markup_fn("bad", video_id=item_id)

        if mode == "vseg" and not ok:
            send_message(reply, chat_id=str(chat_id))
            return

        try:
            api(
                "editMessageReplyMarkup",
                {"chat_id": chat_id, "message_id": message_id, "reply_markup": markup},
                timeout=15,
            )
        except Exception as exc:
            log.warning("edit markup failed data=%s: %s", data, exc)
    except Exception as exc:
        log.exception("callback failed data=%s: %s", data, exc)
        try:
            api(
                "answerCallbackQuery",
                {"callback_query_id": query_id, "text": str(exc)[:180], "show_alert": True},
                timeout=15,
            )
        except Exception:
            pass
