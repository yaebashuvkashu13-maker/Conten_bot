#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pubg_vod_singles_first import (  # noqa: E402
    pick_next_single_row,
    pubg_singles_first_enabled,
    singles_keyboard,
)


def test_singles_first_enabled_by_default(monkeypatch):
    monkeypatch.delenv("PUBG_VOD_SINGLES_FIRST", raising=False)
    assert pubg_singles_first_enabled() is True


def test_pick_next_single_marks_final_when_one_left():
    rows = [
        {"segment_id": "vid_100", "peak_start": 100.0, "start": 95.0, "score": 0.9},
        {"segment_id": "vid_200", "peak_start": 200.0, "start": 195.0, "score": 0.8},
    ]

    def _close(peak, used, gap):
        return any(abs(peak - u) <= gap for u in used)

    row, is_final = pick_next_single_row(
        rows,
        blocked_ids=set(),
        rejected_peaks=[],
        gap_sec=45.0,
        used_peaks=[],
        peak_too_close=_close,
    )
    assert row["segment_id"] == "vid_100"
    assert is_final is False

    row2, is_final2 = pick_next_single_row(
        rows,
        blocked_ids={"vid_100"},
        rejected_peaks=[],
        gap_sec=45.0,
        used_peaks=[100.0],
        peak_too_close=_close,
    )
    assert row2["segment_id"] == "vid_200"
    assert is_final2 is True


def test_assemble_keyboard_on_final():
    markup = singles_keyboard("pubg", "vid_100", "vid", show_assemble=True)
    texts = [btn["text"] for row in markup["inline_keyboard"] for btn in row]
    assert "🔧 Собрать склейку" in texts
    assert "⏭ Пропустить" in texts
