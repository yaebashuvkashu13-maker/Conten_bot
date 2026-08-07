#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_banner_ref_match import (  # noqa: E402
    clear_banner_ref_cache,
    extract_banner_zone_patch,
    match_banner_reference,
    patch_similarity,
)


def test_patch_similarity_identical() -> None:
    patch = np.zeros((48, 160, 3), dtype=np.uint8)
    patch[:, :, 0] = 200
    patch[:, :, 1] = 120
    assert patch_similarity(patch, patch) > 0.99


def test_wiki_assets_match_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use checked-in wiki assets — no network download in CI."""
    repo = Path(__file__).resolve().parents[1]
    wiki = repo / "data" / "mlbb_kill_banners"
    assert (wiki / "wiki" / "classic.png").exists()
    monkeypatch.setenv("MLBB_BANNER_REF_ROOT", str(wiki))
    monkeypatch.setenv("MLBB_BANNER_OWNER_REFS", "0")
    monkeypatch.setenv("MLBB_BANNER_REF_MATCH", "1")
    monkeypatch.setenv("MLBB_BANNER_REF_MIN_SIM", "0.30")
    clear_banner_ref_cache()

    wiki_png = wiki / "wiki" / "classic.png"
    ref = cv2.imread(str(wiki_png))
    assert ref is not None
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    h, w = frame.shape[:2]
    y0, y1 = int(h * 0.02), int(h * 0.30)
    x0, x1 = int(w * 0.15), int(w * 0.85)
    resized = cv2.resize(ref, (x1 - x0, y1 - y0))
    frame[y0:y1, x0:x1] = resized

    row = match_banner_reference(frame)
    assert row is not None
    score, name, source, tier = row
    assert score >= 0.30
    assert source in ("wiki", "glob")
    assert tier >= 2


def test_extract_banner_zone_patch_shape() -> None:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    patch = extract_banner_zone_patch(frame)
    assert patch is not None
    assert patch.shape == (48, 160, 3)


def test_negative_owner_cal_rejects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner-labeled no_banner crops must block false positive wiki matches."""
    import mlbb_banner_ref_match as m

    repo = Path(__file__).resolve().parents[1]
    wiki = repo / "data" / "mlbb_kill_banners"
    root = tmp_path / "refs"
    (root / "wiki").mkdir(parents=True)
    # Copy one wiki asset as positive-looking bank entry.
    import shutil

    shutil.copy(wiki / "wiki" / "classic.png", root / "wiki" / "classic.png")
    (root / "manifest.json").write_text(
        '{"refs":[{"path":"wiki/classic.png","name":"classic","source":"wiki","tier_hint":"double"}]}',
        encoding="utf-8",
    )
    # Build a frame that matches wiki.
    ref = cv2.imread(str(wiki / "wiki" / "classic.png"))
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    h, w = frame.shape[:2]
    y0, y1 = int(h * 0.02), int(h * 0.30)
    x0, x1 = int(w * 0.15), int(w * 0.85)
    frame[y0:y1, x0:x1] = cv2.resize(ref, (x1 - x0, y1 - y0))

    # Negative bank: same patch labeled no_banner → must reject.
    neg_dir = root / "owner_cal" / "negative" / "no_banner"
    neg_dir.mkdir(parents=True)
    patch = extract_banner_zone_patch(frame)
    assert patch is not None
    cv2.imwrite(str(neg_dir / "fake.png"), cv2.resize(patch, (320, 96)))

    monkeypatch.setenv("MLBB_BANNER_REF_ROOT", str(root))
    monkeypatch.setenv("MLBB_BANNER_OWNER_REFS", "0")
    monkeypatch.setenv("MLBB_BANNER_REF_MATCH", "1")
    monkeypatch.setenv("MLBB_BANNER_NEG_REF_MATCH", "1")
    monkeypatch.setenv("MLBB_BANNER_REF_MIN_SIM", "0.20")
    monkeypatch.setenv("MLBB_BANNER_NEG_REF_MIN_SIM", "0.20")
    monkeypatch.setenv("MLBB_BANNER_NEG_POS_MARGIN", "0.00")
    monkeypatch.setattr(m, "owner_photo_root", lambda: tmp_path / "no_photos")
    m.clear_banner_ref_cache()

    assert match_banner_reference(frame) is None

