"""Banner OCR: HAS SLAIN / MEGA KILL aliases and streak-slice cleanup."""

from __future__ import annotations

from mlbb_banner_ocr import fuzzy_match_banner_label, _prefer_streak_slice


def test_fuzzy_has_slain_without_been() -> None:
    hit = fuzzy_match_banner_label(
        "37ms 14 09:05 10 19 X314 HAS SLAIN 10 3+14 HAS SLAIN junk"
    )
    assert hit is not None
    assert hit[2] == 1
    assert hit[3] == "single"


def test_fuzzy_hasslain_glued() -> None:
    hit = fuzzy_match_banner_label("ss 12 12:50 5536/6118 HASSLAIN")
    assert hit is not None
    assert hit[3] == "single"


def test_fuzzy_mega_kill() -> None:
    hit = fuzzy_match_banner_label("30ms 15 09:13 X414 MEGA KIL 10 x4+14")
    assert hit is not None
    assert hit[2] >= 2


def test_prefer_streak_slice_drops_hud_soup() -> None:
    raw = "37ms 14 09:05 10 19 16 1931 X314 HAS SLAIN 10 3+14 × HAS SLAIN junk"
    sliced = _prefer_streak_slice(raw)
    assert "HAS SLAIN" in sliced.upper()
    assert "1931" not in sliced or len(sliced) < len(raw)
