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
        raise RuntimeError(f"Telegram API error for {method}: {result}")
    return result["result"]


def schedule_mlbb_retrain() -> None:
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
    """Return (mode, is_good, item_id, reason). mode: shorts|vseg|noop|unknown."""
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
    return "unknown", None, "", ""


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
    if mode == "unknown" or is_good is None:
        try:
            api("answerCallbackQuery", {"callback_query_id": query_id}, timeout=15)
        except Exception:
            pass
        return

    try:
        if mode == "vseg":
            ok, reply = apply_vseg_label(chat_id, item_id, is_good=is_good, reason=reason)
            from mlbb_vod_segment_store import labeled_keyboard_markup as markup_fn

            markup = markup_fn("good" if is_good else "bad")
        else:
            ok, reply = apply_shorts_label(chat_id, item_id, is_good=is_good, reason=reason)
            from mlbb_calibration_store import labeled_keyboard_markup as markup_fn

            markup = markup_fn("good" if is_good else "bad")

        if not ok:
            api(
                "answerCallbackQuery",
                {"callback_query_id": query_id, "text": reply[:180], "show_alert": True},
                timeout=15,
            )
            return

        api(
            "answerCallbackQuery",
            {"callback_query_id": query_id, "text": "✅ Ок" if is_good else "❌ Не ок"},
            timeout=15,
        )
        api(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": markup},
            timeout=15,
        )
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
