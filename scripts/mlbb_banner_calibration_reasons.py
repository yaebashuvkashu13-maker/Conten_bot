#!/usr/bin/env python3
"""Owner reason buttons for MLBB kill-banner screenshot calibration."""

from __future__ import annotations

# code, short callback token (≤8 chars), button label
BANNER_CALIB_REASONS: tuple[tuple[str, str, str], ...] = (
    ("no_banner", "nb", "❌ Нет банера"),
    ("not_kill", "nk", "⚠️ Банер не с убийством"),
    ("wrong_hero", "wh", "🦸 Не тот герой"),
    ("enemy_kill", "ek", "👿 Kill противника"),
    ("not_enemy_kill", "ne", "✅ Не kill противника"),
    ("not_gameplay", "ng", "🎬 Не геймплей"),
    # Owner-suggested extras for clearer positive anchors
    ("own_kill_good", "ok", "✅ Свой kill — ок"),
    ("savage_tier", "sv", "🔥 Savage/Legendary"),
    ("double_triple", "dt", "⚡ Double/Triple/Maniac"),
)

REASON_CODES: set[str] = {code for code, _, _ in BANNER_CALIB_REASONS}
SHORT_TO_REASON: dict[str, str] = {short: code for code, short, _ in BANNER_CALIB_REASONS}
REASON_TO_SHORT: dict[str, str] = {code: short for code, short, _ in BANNER_CALIB_REASONS}
REASON_LABELS: dict[str, str] = {code: label for code, _, label in BANNER_CALIB_REASONS}

# Positive labels → add banner crop to reference bank (vod_crops).
POSITIVE_REASONS: set[str] = {"not_enemy_kill", "own_kill_good", "savage_tier", "double_triple"}

# Hard negatives — reject similar HUD patches during detection.
NEGATIVE_REASONS: set[str] = {"no_banner", "not_kill", "wrong_hero", "enemy_kill", "not_gameplay"}

TIER_FOR_REASON: dict[str, str] = {
    "savage_tier": "savage",
    "double_triple": "triple",
    "own_kill_good": "unknown",
    "not_enemy_kill": "unknown",
}


def reason_label(code: str) -> str:
    return REASON_LABELS.get(code, code)


def reason_from_short(short: str) -> str | None:
    return SHORT_TO_REASON.get(short.strip())


def inline_keyboard_markup(check_id: str) -> dict:
    """One-shot reason picker — each button records the label immediately."""
    cid = check_id.strip()
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for code, short, label in BANNER_CALIB_REASONS:
        cb = f"mlbb_bcal:{cid}:{short}"
        if len(cb) > 64:
            raise ValueError(f"callback_data too long: {cb}")
        row.append({"text": label, "callback_data": cb})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


def labeled_keyboard_markup(reason: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": f"✓ {reason_label(reason)}", "callback_data": "mlbb_noop"}],
        ]
    }
