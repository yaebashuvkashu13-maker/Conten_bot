#!/usr/bin/env python3
"""Per-game 👎 reason pickers for Shorts / VOD calibration."""

from __future__ import annotations

# code, button label
GAME_DISLIKE_REASONS: dict[str, tuple[tuple[str, str], ...]] = {
    "mlbb": (
        ("promo", "📢 Реклама"),
        ("not_gameplay", "🎬 Не геймплей"),
        ("boring", "😴 Скучно / нет боя"),
        ("wrong_hero", "🦸 Не тот герой"),
        ("no_kill", "💀 Нет килла / баннера"),
        ("music", "🎵 Музыка"),
        ("blurry", "🌫 Мыльное"),
        ("other", "🗑 Другое"),
    ),
    "mobile_legends": (),  # alias filled below
    "pubg": (
        ("not_metro", "🚇 Не Metro Royale"),
        ("classic", "🌤 Классика / небо"),
        ("no_combat", "🔇 Нет перестрелки"),
        ("no_kill", "💀 Килл не виден"),
        ("author_death", "☠️ Автора убивают"),
        ("loot_run", "🎒 Бег / лут без боя"),
        ("promo", "📢 Реклама"),
        ("boring", "😴 Скучно"),
        ("other", "🗑 Другое"),
    ),
    "standoff": (
        ("not_gameplay", "🎬 Не геймплей"),
        ("no_gunfire", "🔇 Нет стрельбы"),
        ("no_kill", "💀 Килл автора не виден"),
        ("author_death", "☠️ Автора убивают"),
        ("menu_lobby", "📋 Меню / лобби"),
        ("promo", "📢 Реклама"),
        ("boring", "😴 Скучно"),
        ("blurry", "🌫 Мыльное"),
        ("wrong_mode", "⚙️ Не ranked / не бой"),
        ("other", "🗑 Другое"),
    ),
    "genshin": (
        ("not_boss", "👹 Не босс / не бой"),
        ("dialogue", "💬 Диалог / катсцена"),
        ("explore", "🗺 Бег / исследование"),
        ("promo", "📢 Реклама"),
        ("boring", "😴 Скучно"),
        ("blurry", "🌫 Мыльное"),
        ("wrong_element", "✨ Не тот контент"),
        ("other", "🗑 Другое"),
    ),
    "wot": (
        ("no_hit", "💥 Нет попадания / взрыва"),
        ("menu_garage", "🔧 Ангар / меню"),
        ("not_gameplay", "🎬 Не геймплей"),
        ("promo", "📢 Реклама"),
        ("boring", "😴 Скучно"),
        ("blurry", "🌫 Мыльное"),
        ("wrong_tank", "🛡 Не бой танков"),
        ("other", "🗑 Другое"),
    ),
}

GAME_DISLIKE_REASONS["mobile_legends"] = GAME_DISLIKE_REASONS["mlbb"]

GAME_LABELS: dict[str, str] = {
    "mlbb": "MLBB",
    "mobile_legends": "MLBB",
    "pubg": "PUBG Metro Royale",
    "standoff": "Standoff 2",
    "genshin": "Genshin Impact",
    "wot": "World of Tanks",
}


def normalize_game(game: str) -> str:
    g = (game or "").strip().lower()
    if g in ("mlbb", "mobile_legends"):
        return "mlbb"
    return g


def dislike_reasons_for_game(game: str) -> tuple[tuple[str, str], ...]:
    g = normalize_game(game)
    return GAME_DISLIKE_REASONS.get(g, GAME_DISLIKE_REASONS["mlbb"])


def dislike_reason_codes(game: str) -> set[str]:
    return {code for code, _ in dislike_reasons_for_game(game)}


def dislike_reason_label(game: str, reason: str) -> str:
    for code, label in dislike_reasons_for_game(game):
        if code == reason:
            return label
    return reason.strip() or "Плохо"


def dislike_reason_keyboard_markup(
    item_id: str,
    *,
    game: str,
    callback_prefix: str | None = None,
) -> dict:
    g = normalize_game(game)
    prefix = callback_prefix or f"{g}_bad"
    vid = str(item_id).strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for code, label in dislike_reasons_for_game(g):
        row.append({"text": label, "callback_data": f"{prefix}:{vid}:{code}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


def shorts_keyboard_markup(video_id: str, *, game: str) -> dict:
    g = normalize_game(game)
    vid = str(video_id).strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    return {
        "inline_keyboard": [
            [
                {"text": "👍", "callback_data": f"{g}_yes:{vid}"},
                {"text": "👎", "callback_data": f"{g}_no:{vid}"},
            ],
        ]
    }


def labeled_keyboard_markup(
    game: str,
    label: str,
    *,
    reason: str = "",
    video_id: str = "",
) -> dict:
    g = normalize_game(game)
    if label == "good":
        vid = str(video_id).strip()
        if vid.startswith("yt_"):
            vid = vid[3:]
        rows: list[list[dict]] = [[{"text": "✅ Хорошо", "callback_data": f"{g}_noop"}]]
        if vid:
            rows.append([{"text": "📁 HQ файл", "callback_data": f"{g}_hq:{vid}"}])
            try:
                from social_publish import social_button_row

                rows.append(social_button_row(g, vid))
            except Exception:
                pass
        return {"inline_keyboard": rows}
    mark = f"❌ {dislike_reason_label(g, reason)}"
    return {"inline_keyboard": [[{"text": mark, "callback_data": f"{g}_noop"}]]}


def feedback_ack_message(game: str, *, is_good: bool, reason: str = "") -> str:
    g = normalize_game(game)
    name = GAME_LABELS.get(g, g.upper())
    if is_good:
        return f"✅ {name}: записал как хороший пример (вес для CLIP/exemplars)."
    detail = dislike_reason_label(g, reason)
    return f"❌ {name}: {detail}\nЗаписано в обучение — этот тип клипов будет штрафоваться."
