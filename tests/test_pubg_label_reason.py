#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_label_reason import normalize_reason, weight_multiplier  # noqa: E402


def test_normalize_reason_codes_and_notes() -> None:
    assert normalize_reason("loot_run") == "loot_run"
    assert normalize_reason("лут без боя") == "loot_run"
    assert normalize_reason("menu_lobby") == "menu_lobby"
    assert normalize_reason("run no starter shots") == "loot_run"
    assert normalize_reason("normal fight act — do not skip") == "style_ref"
    assert normalize_reason("target fight style — part 2 reference") == "style_ref"
    assert normalize_reason("fight") == "fight"


def test_weight_multiplier_stronger_for_hard_negatives() -> None:
    assert weight_multiplier("loot_run", label=0) > weight_multiplier("", label=0)
    assert weight_multiplier("fight", label=1) > 1.0
    assert weight_multiplier("loot_run", label=1) < 1.0  # conflict
