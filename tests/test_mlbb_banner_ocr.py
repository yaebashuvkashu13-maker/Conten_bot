#!/usr/bin/env python3
"""Banner OCR read path: fuzzy labels + ref requires live streak text."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_fuzzy_sawage_is_savage() -> None:
    from mlbb_banner_ocr import fuzzy_match_banner_label

    hit = fuzzy_match_banner_label("25 19 X1524 SAWAGE")
    assert hit is not None
    assert hit[3] == "savage"
    assert hit[2] == 5


def test_fuzzy_ustene_is_unstoppable() -> None:
    from mlbb_banner_ocr import fuzzy_match_banner_label

    hit = fuzzy_match_banner_label("USTENE")
    assert hit is not None
    assert hit[3] == "double"
    assert "UNSTOPPABLE" in hit[1]


def test_fuzzy_rejects_short_junk() -> None:
    from mlbb_banner_ocr import fuzzy_match_banner_label

    assert fuzzy_match_banner_label("i") is None
    assert fuzzy_match_banner_label("617") is None
    assert fuzzy_match_banner_label("00:39 CindyTom") is None


def test_classify_banner_text_fuzzy_garbage() -> None:
    from mlbb_kill_banner import classify_banner_text

    hit = classify_banner_text("X1524 SAWAGE 8")
    assert hit is not None
    assert hit.label == "savage"
    assert hit.tier == 5


def test_finalize_allows_ref_when_live_is_player_names(monkeypatch) -> None:
    """HUD chrome (names) must not veto a ref hit — that starved all sends."""
    import mlbb_kill_banner as kb

    monkeypatch.setenv("MLBB_BANNER_OWN_KILL_REQUIRED", "1")
    monkeypatch.setenv("MLBB_BANNER_REF_REQUIRE_OCR", "1")
    monkeypatch.setenv("MLBB_BANNER_REF_REQUIRE_OCR_STRICT", "0")
    hit = kb.KillBannerHit(sec=58.0, tier=3, label="triple", text="TRIPLE KILL", source="ref")

    with (
        patch.object(kb, "_live_overlay_text", return_value="SUUSO CarterMejor CindyTom"),
        patch(
            "mlbb_banner_hero_match.validate_own_kill_frame",
            return_value=(True, "hud_killer_ok"),
        ),
    ):
        out = kb._finalize_banner_hit(object(), hit, vod=None)
        assert out is not None
        assert out.tier == 3


def test_finalize_rejects_ref_when_live_is_farm_names(monkeypatch) -> None:
    """Strict mode still drops canned TRIPLE when live OCR has no streak."""
    import mlbb_kill_banner as kb

    monkeypatch.setenv("MLBB_BANNER_OWN_KILL_REQUIRED", "1")
    monkeypatch.setenv("MLBB_BANNER_LIVE_OVERLAY_OCR", "1")
    monkeypatch.setenv("MLBB_BANNER_REF_REQUIRE_OCR", "1")
    monkeypatch.setenv("MLBB_BANNER_REF_REQUIRE_OCR_STRICT", "1")
    hit = kb.KillBannerHit(sec=58.0, tier=3, label="triple", text="TRIPLE KILL", source="ref")

    with (
        patch.object(kb, "_live_overlay_text", return_value="SUUSO CarterMejor CindyTom"),
        patch("mlbb_banner_hero_match.validate_own_kill_frame") as mock_own,
    ):
        out = kb._finalize_banner_hit(object(), hit, vod=None)
        assert out is None
        mock_own.assert_not_called()


def test_finalize_upgrades_ref_from_live_savage(monkeypatch) -> None:
    import mlbb_kill_banner as kb

    monkeypatch.setenv("MLBB_BANNER_OWN_KILL_REQUIRED", "1")
    monkeypatch.setenv("MLBB_BANNER_LIVE_OVERLAY_OCR", "1")
    monkeypatch.setenv("MLBB_BANNER_REF_REQUIRE_OCR", "1")
    hit = kb.KillBannerHit(sec=12.0, tier=3, label="triple", text="TRIPLE KILL", source="ref")

    with (
        patch.object(kb, "_live_overlay_text", return_value="X1524 SAWAGE"),
        patch(
            "mlbb_banner_hero_match.validate_own_kill_frame",
            return_value=(True, "hud_killer_ok"),
        ),
    ):
        out = kb._finalize_banner_hit(object(), hit, vod=None)
        assert out is not None
        assert out.label == "savage"
        assert out.tier == 5
        assert "ocr" in out.source


def test_finalize_allows_ref_on_ocr_silence(monkeypatch) -> None:
    """Empty live OCR keeps prior ref gates (don't fail closed on silence)."""
    import mlbb_kill_banner as kb

    monkeypatch.setenv("MLBB_BANNER_OWN_KILL_REQUIRED", "1")
    monkeypatch.setenv("MLBB_BANNER_REF_REQUIRE_OCR", "1")
    hit = kb.KillBannerHit(sec=12.0, tier=3, label="triple", text="TRIPLE KILL", source="ref")

    with (
        patch.object(kb, "_live_overlay_text", return_value=""),
        patch(
            "mlbb_banner_hero_match.validate_own_kill_frame",
            return_value=(True, "hud_killer_ok"),
        ),
    ):
        out = kb._finalize_banner_hit(object(), hit, vod=None)
        assert out is not None
        assert out.tier == 3
