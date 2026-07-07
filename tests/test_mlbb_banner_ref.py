#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_banner_ref_ingest import download_wiki_notifications, write_manifest  # noqa: E402
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


def test_wiki_download_and_match(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_BANNER_REF_ROOT", str(tmp_path))
    clear_banner_ref_cache()
    rows = download_wiki_notifications()
    assert any(r["ok"] for r in rows)
    write_manifest(wiki_rows=rows)
    clear_banner_ref_cache()

    wiki_png = next((tmp_path / "wiki").glob("*.png"))
    ref = cv2.imread(str(wiki_png))
    assert ref is not None
    # Fake gameplay frame: paste wiki preview into banner zone.
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    h, w = frame.shape[:2]
    y0, y1 = int(h * 0.02), int(h * 0.30)
    x0, x1 = int(w * 0.15), int(w * 0.85)
    resized = cv2.resize(ref, (x1 - x0, y1 - y0))
    frame[y0:y1, x0:x1] = resized

    row = match_banner_reference(frame)
    assert row is not None
    score, name, source, tier = row
    assert score >= 0.38
    assert source in ("wiki", "glob")
    assert tier >= 2


def test_extract_banner_zone_patch_shape() -> None:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    patch = extract_banner_zone_patch(frame)
    assert patch is not None
    assert patch.shape == (48, 160, 3)
