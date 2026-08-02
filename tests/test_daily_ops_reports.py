#!/usr/bin/env python3
"""Tests for live morning/evening ops report formatters."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from daily_ops_stats import format_evening, format_morning  # noqa: E402


def _snap(**overrides):
    base = {
        "day": "2026-07-29",
        "hm": "09:00",
        "active_game": "mlbb",
        "sends": {"mlbb": 1, "pubg": 5, "standoff": 5, "genshin": 5, "wot": 5},
        "quotas": {"mlbb": 5, "pubg": 5, "standoff": 5, "genshin": 5, "wot": 5},
        "remaining": {"mlbb": 4, "pubg": 0, "standoff": 0, "genshin": 0, "wot": 0},
        "total_sent": 21,
        "total_quota": 25,
        "feedback": {
            "mlbb": {"yes": 2, "no": 1, "reasons": {"run": 1}},
            "pubg": {"yes": 3, "no": 0, "reasons": {}},
            "standoff": {"yes": 1, "no": 1, "reasons": {"boring": 1}},
            "genshin": {"yes": 0, "no": 0, "reasons": {}},
            "wot": {"yes": 0, "no": 0, "reasons": {}},
        },
        "total_yes": 6,
        "total_no": 2,
        "inbox": {"mlbb": 3, "pubg": 2, "standoff": 1, "genshin": 0, "wot": 4},
        "montages": [
            {
                "game": "standoff",
                "vod_id": "abc",
                "montage_id": "standoff_m1",
                "peaks": [10, 20, 30],
                "at": "2026-07-29T18:00:00+03:00",
            }
        ],
        "skipped": {"mlbb": {"reason": "discovery_miss", "at": "12:00"}},
        "discovery_misses": {"mlbb": 4},
        "catchup_done": True,
        "catchup_games": ["mlbb"],
        "catchup_at": "2026-07-29T15:00:00+03:00",
        "procs": {"cycle": True, "mlbb_feed": True, "feed_shell": True},
        "log": {
            "sent_lines": 12,
            "montage": 1,
            "rejects": {"ocr_single_reject": 3, "banner_ctx_run": 2},
        },
        "montage_on": True,
        "montage_only": False,
        "post_quota": True,
    }
    base.update(overrides)
    return base


def test_morning_is_live_not_shorts_template():
    text = format_morning(_snap())
    assert "Утро 2026-07-29" in text
    assert "21/25" in text
    assert "MLBB 1/5" in text
    assert "склейки" in text.lower() or "MLBB склейки" in text
    assert "только MLBB" not in text
    assert "Калибровка Shorts" not in text
    assert "Другие игры отключены" not in text
    assert "без склейки" not in text
    assert "Catch-up" in text
    assert "План" in text


def test_evening_shows_feedback_and_montages():
    text = format_evening(_snap(hm="21:00"))
    assert "Вечер 2026-07-29" in text
    assert "21/25" in text
    assert "👍6" in text or "👍6 / 👎2" in text
    assert "standoff_m1" in text or "Standoff" in text
    assert "ocr-single" in text.lower() or "OCR-single" in text
    assert "Калибровка Shorts" not in text
    assert "Другие игры отключены" not in text
    assert "На завтра" in text


def test_evening_closed_day_verdict():
    text = format_evening(
        _snap(
            hm="21:00",
            sends={"mlbb": 5, "pubg": 5, "standoff": 5, "genshin": 5, "wot": 5},
            remaining={"mlbb": 0, "pubg": 0, "standoff": 0, "genshin": 0, "wot": 0},
            total_sent=25,
            active_game=None,
            skipped={},
            discovery_misses={},
            inbox={"mlbb": 1, "pubg": 1, "standoff": 1, "genshin": 1, "wot": 1},
            log={"sent_lines": 25, "montage": 2, "rejects": {}},
        )
    )
    assert "День закрыт по квотам" in text
    assert "критичных нет" in text
