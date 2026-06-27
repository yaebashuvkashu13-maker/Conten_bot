#!/usr/bin/env python3
"""PUBG owner 👎 reason codes (Shorts + VOD segments)."""

from __future__ import annotations

DISLIKE_REASONS: tuple[tuple[str, str], ...] = (
    ("not_metro", "🚇 Не Metro"),
    ("classic", "🌤 Classic PUBG"),
    ("not_combat", "🔫 Не бой"),
    ("promo", "📢 Реклама"),
    ("boring", "😴 Скучно"),
    ("music", "🎵 Музыка"),
    ("other", "🗑 Другое"),
)

DISLIKE_REASON_CODES = {code for code, _ in DISLIKE_REASONS}


def dislike_reason_label(reason: str) -> str:
    for code, label in DISLIKE_REASONS:
        if code == reason:
            return label
    return reason.strip() or "Плохо"


def dislike_reason_keyboard_markup(item_id: str, *, callback_prefix: str) -> dict:
    vid = str(item_id).strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    rows: list[list[dict[str, str]]] = []
    # «Не Metro» — отдельная первая строка (самый частый кейс).
    rows.append([{"text": "🚇 Не Metro", "callback_data": f"{callback_prefix}:{vid}:not_metro"}])
    row: list[dict[str, str]] = []
    for code, label in DISLIKE_REASONS:
        if code == "not_metro":
            continue
        row.append({"text": label, "callback_data": f"{callback_prefix}:{vid}:{code}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}
