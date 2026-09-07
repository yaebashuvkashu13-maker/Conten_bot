#!/usr/bin/env python3
"""Telegram spam of 15s VOD windows for owner fight/not-fight labeling.

Buttons:
  👍 Файт  → good + event=fight
  👎 Не ок → reason picker → bad + event
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent

WINDOW_BAD_REASONS: tuple[tuple[str, str], ...] = (
    ("loot", "🎒 Лут/бег"),
    ("menu", "📋 Меню"),
    ("no_combat", "🔇 Нет боя"),
    ("boring", "😴 Скучно"),
    ("other", "🗑 Другое"),
)


def _load_env() -> None:
    env_path = Path(os.environ.get("VIDEO_BOT_ENV", "/root/.video_bot.env"))
    if not env_path.is_file():
        return
    for line in env_path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def queue_path() -> Path:
    return Path(
        os.environ.get(
            "PUBG_WINDOW_LABEL_QUEUE",
            "/root/data/pubg/window_label_queue.jsonl",
        )
    )


def pending_path() -> Path:
    return Path(
        os.environ.get(
            "PUBG_WINDOW_LABEL_PENDING",
            "/root/data/pubg/window_label_pending.json",
        )
    )


def preview_dir() -> Path:
    return Path(
        os.environ.get(
            "PUBG_WINDOW_PREVIEW_DIR",
            "/root/data/pubg/window_previews",
        )
    )


def window_id(video_id: str, t0: float, t1: float) -> str:
    raw = f"{video_id}:{round(float(t0), 1)}:{round(float(t1), 1)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def load_pending() -> dict[str, Any]:
    path = pending_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_pending(data: dict[str, Any]) -> None:
    path = pending_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_queue_rows() -> list[dict[str, Any]]:
    path = queue_path()
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def rewrite_queue(rows: list[dict[str, Any]]) -> None:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)


def yes_no_keyboard(wid: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "👍 Файт", "callback_data": f"pubg_win_yes:{wid}"},
                {"text": "👎 Не ок", "callback_data": f"pubg_win_no:{wid}"},
            ]
        ]
    }


def bad_reason_keyboard(wid: str) -> dict:
    rows: list[list[dict]] = []
    row: list[dict] = []
    for code, label in WINDOW_BAD_REASONS:
        row.append({"text": label, "callback_data": f"pubg_win_bad:{wid}:{code}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


def labeled_keyboard(is_good: bool, reason: str = "") -> dict:
    if is_good:
        mark = "✅ Файт"
    else:
        label = next((lab for code, lab in WINDOW_BAD_REASONS if code == reason), reason or "Не ок")
        mark = f"❌ {label}"
    return {"inline_keyboard": [[{"text": mark, "callback_data": "pubg_win_noop"}]]}


def _token_chat() -> tuple[str, str]:
    _load_env()
    token = (os.environ.get("TG_BOT_TOKEN") or "").strip()
    chat = (
        os.environ.get("PUBG_OWNER_CHAT_ID")
        or os.environ.get("TG_CHAT_ID")
        or ""
    ).strip()
    if not token or not chat:
        raise RuntimeError("TG_BOT_TOKEN / TG_CHAT_ID missing")
    return token, chat


def send_next_windows(*, limit: int = 5, pause_sec: float = 1.0) -> dict[str, Any]:
    """Cut pending queue windows and send to owner with 👍/👎."""
    from mlbb_telegram_video import send_video_file
    from pubg_window_label_queue import render_preview

    token, chat_id = _token_chat()
    rows = load_queue_rows()
    pending = load_pending()
    sent = 0
    errors: list[str] = []
    changed = False

    for row in rows:
        if sent >= limit:
            break
        if str(row.get("status") or "pending") != "pending":
            continue
        try:
            video_id = str(row["video_id"])
            t0 = float(row["t0"])
            t1 = float(row["t1"])
            vod = Path(str(row.get("video_path") or ""))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"bad_row:{exc}")
            continue
        if not vod.is_file():
            row["status"] = "missing_vod"
            changed = True
            errors.append(f"missing:{video_id}")
            continue

        wid = window_id(video_id, t0, t1)
        if pending.get(wid, {}).get("status") == "labeled":
            row["status"] = "labeled"
            changed = True
            continue

        dest = preview_dir() / f"win_{wid}.mp4"
        try:
            render_preview(vod, t0, t1, dest)
        except Exception as exc:
            errors.append(f"preview:{wid}:{exc}")
            continue

        caption = (
            f"🎓 Разметка окна (обучение)\n"
            f"VOD `{video_id}`\n"
            f"{t0:.0f}–{t1:.0f}s (~15 сек)\n"
            f"👍 = файт / годный момент\n"
            f"👎 = не то (потом причина)"
        )
        ok = send_video_file(
            token,
            chat_id,
            dest,
            caption,
            reply_markup=yes_no_keyboard(wid),
        )
        if not ok:
            errors.append(f"send:{wid}")
            continue

        pending[wid] = {
            "video_id": video_id,
            "t0": t0,
            "t1": t1,
            "peak_sec": round((t0 + t1) * 0.5, 2),
            "video_path": str(vod),
            "preview_path": str(dest),
            "status": "sent",
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        row["status"] = "sent"
        row["window_id"] = wid
        changed = True
        sent += 1
        if pause_sec > 0:
            time.sleep(pause_sec)

    if changed:
        rewrite_queue(rows)
        save_pending(pending)

    return {
        "status": "ok",
        "sent": sent,
        "pending_left": sum(1 for r in rows if r.get("status") == "pending"),
        "errors": errors[:10],
    }


def apply_window_callback(
    data: str,
    *,
    chat_id: str | int,
    message_id: int | None,
    query_id: str,
    api_call,
) -> bool:
    """Handle pubg_win_* callbacks. Returns True if consumed."""
    if data == "pubg_win_noop":
        api_call("answerCallbackQuery", {"callback_query_id": query_id}, timeout=15)
        return True
    if not (
        data.startswith("pubg_win_yes:")
        or data.startswith("pubg_win_no:")
        or data.startswith("pubg_win_bad:")
    ):
        return False

    from pubg_ranker_dataset import append_window_label

    pending = load_pending()

    if data.startswith("pubg_win_no:"):
        wid = data.split(":", 1)[1].strip()
        if wid not in pending:
            api_call(
                "answerCallbackQuery",
                {"callback_query_id": query_id, "text": "Окно не найдено", "show_alert": True},
                timeout=15,
            )
            return True
        api_call(
            "answerCallbackQuery",
            {"callback_query_id": query_id, "text": "Выбери причину 👇"},
            timeout=15,
        )
        if message_id is not None:
            try:
                api_call(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": str(chat_id),
                        "message_id": int(message_id),
                        "reply_markup": bad_reason_keyboard(wid),
                    },
                    timeout=30,
                )
            except Exception:
                pass
        return True

    is_good = data.startswith("pubg_win_yes:")
    reason = ""
    if is_good:
        wid = data.split(":", 1)[1].strip()
        event = "fight"
    else:
        # pubg_win_bad:wid:reason
        parts = data.split(":")
        if len(parts) < 3:
            api_call(
                "answerCallbackQuery",
                {"callback_query_id": query_id, "text": "Плохой callback", "show_alert": True},
                timeout=15,
            )
            return True
        wid = parts[1].strip()
        reason = parts[2].strip()
        event = reason or "other"

    row = pending.get(wid)
    if not row:
        api_call(
            "answerCallbackQuery",
            {"callback_query_id": query_id, "text": "Окно не найдено", "show_alert": True},
            timeout=15,
        )
        return True

    append_window_label(
        video_id=str(row["video_id"]),
        t0=float(row["t0"]),
        t1=float(row["t1"]),
        label="good" if is_good else "bad",
        event=event,
        video_path=str(row.get("video_path") or ""),
        source="window_tg",
    )
    row["status"] = "labeled"
    row["label"] = "good" if is_good else "bad"
    row["event"] = event
    row["labeled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    pending[wid] = row
    save_pending(pending)

    # Mark queue row labeled
    qrows = load_queue_rows()
    changed = False
    for q in qrows:
        if q.get("window_id") == wid or (
            str(q.get("video_id")) == str(row["video_id"])
            and abs(float(q.get("t0", -1)) - float(row["t0"])) < 0.2
        ):
            q["status"] = "labeled"
            changed = True
    if changed:
        rewrite_queue(qrows)

    api_call(
        "answerCallbackQuery",
        {"callback_query_id": query_id, "text": "✅ Записано" if is_good else "❌ Записано"},
        timeout=15,
    )
    if message_id is not None:
        try:
            api_call(
                "editMessageReplyMarkup",
                {
                    "chat_id": str(chat_id),
                    "message_id": int(message_id),
                    "reply_markup": labeled_keyboard(is_good, reason),
                },
                timeout=30,
            )
        except Exception:
            pass
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Send PUBG window-label spam to Telegram")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--pause", type=float, default=1.0)
    args = parser.parse_args()
    report = send_next_windows(limit=max(1, args.limit), pause_sec=max(0.0, args.pause))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
