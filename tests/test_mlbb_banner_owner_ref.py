"""Owner screenshot bank drives live banner discovery."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_banner_ref_match import (  # noqa: E402
    _load_positive_owner_ref_rows,
    _tier_from_owner_reason,
    classify_banner_reference,
    clear_banner_ref_cache,
)


def test_tier_from_owner_reason() -> None:
    assert _tier_from_owner_reason("savage_tier") == 5
    assert _tier_from_owner_reason("double_triple") == 3
    assert _tier_from_owner_reason("triple") == 3
    assert _tier_from_owner_reason("double") == 2
    assert _tier_from_owner_reason("own_kill_good") == 2


def test_positive_rows_include_vod_crops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pos = tmp_path / "owner_cal" / "positive" / "double_triple"
    pos.mkdir(parents=True)
    cv2.imwrite(str(pos / "a.png"), np.full((48, 160, 3), 40, dtype=np.uint8))
    crop = tmp_path / "vod_crops" / "savage"
    crop.mkdir(parents=True)
    cv2.imwrite(str(crop / "s.png"), np.full((48, 160, 3), 80, dtype=np.uint8))
    monkeypatch.setenv("MLBB_BANNER_REF_ROOT", str(tmp_path))
    monkeypatch.setenv("MLBB_BANNER_POS_INCLUDE_VOD_CROPS", "1")
    clear_banner_ref_cache()
    rows = _load_positive_owner_ref_rows()
    reasons = {r[1] for r in rows}
    assert "double_triple" in reasons
    assert "savage" in reasons


def test_classify_banner_reference_owner_positive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pos = tmp_path / "owner_cal" / "positive" / "double_triple"
    pos.mkdir(parents=True)
    banner = np.zeros((48, 160, 3), dtype=np.uint8)
    banner[:, :] = (20, 180, 220)  # gold-ish BGR
    # Draw ornate edges so edge similarity is non-zero.
    banner[2:6, :] = (255, 255, 255)
    banner[-6:-2, :] = (255, 255, 255)
    banner[:, 2:6] = (255, 255, 255)
    cv2.imwrite(str(pos / "dt.png"), banner)

    monkeypatch.setenv("MLBB_BANNER_REF_ROOT", str(tmp_path))
    monkeypatch.setenv("MLBB_BANNER_POS_REF_MATCH", "1")
    monkeypatch.setenv("MLBB_BANNER_POS_REF_MIN_SIM", "0.30")
    monkeypatch.setenv("MLBB_BANNER_POS_OWN_KILL_MIN_SIM", "0.30")
    monkeypatch.setenv("MLBB_BANNER_POS_SAVAGE_MIN_SIM", "0.30")
    monkeypatch.setenv("MLBB_BANNER_EDGE_MIN_SIM", "0.10")
    monkeypatch.setenv("MLBB_KILL_BANNER_MIN_TIER", "double")
    monkeypatch.setenv("MLBB_BANNER_NEG_REF_MATCH", "0")
    clear_banner_ref_cache()

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    h, w = frame.shape[:2]
    y0, y1 = int(h * 0.02), int(h * 0.30)
    x0, x1 = int(w * 0.15), int(w * 0.85)
    frame[y0:y1, x0:x1] = cv2.resize(banner, (x1 - x0, y1 - y0))

    hit = classify_banner_reference(100.0, frame)
    assert hit is not None
    assert hit.source == "ref"
    assert hit.tier >= 2
    assert "owner_pos" in hit.text
