#!/usr/bin/env python3
"""Tests for structural (edge) banner reference matching."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_banner_ref_match import (  # noqa: E402
    patch_edge_similarity,
    patch_hist_similarity,
    patch_similarity,
)


def _banner_like(seed: int = 0):
    """Synthetic ornate gold banner strip vs empty gold HUD."""
    rng = np.random.default_rng(seed)
    img = np.zeros((48, 160, 3), dtype=np.uint8)
    # gold base
    img[:, :] = (20, 180, 220)  # BGR-ish gold
    # ornate rectangle + "text" bars unique to banners
    img[10:38, 30:130] = (40, 80, 255)
    img[18:22, 45:115] = (255, 255, 255)
    img[26:30, 50:110] = (200, 200, 255)
    # side circles (portraits)
    rr, cc = np.ogrid[:48, :160]
    left = (rr - 24) ** 2 + (cc - 20) ** 2 <= 14**2
    right = (rr - 24) ** 2 + (cc - 140) ** 2 <= 14**2
    img[left] = (180, 120, 60)
    img[right] = (60, 120, 180)
    noise = rng.integers(0, 12, img.shape, dtype=np.uint8)
    return np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def _empty_hud(seed: int = 1):
    rng = np.random.default_rng(seed)
    img = np.zeros((48, 160, 3), dtype=np.uint8)
    img[:, :] = (25, 170, 210)  # similar gold wash
    # thin top bar only (no ornate banner)
    img[0:6, :] = (30, 200, 240)
    noise = rng.integers(0, 18, img.shape, dtype=np.uint8)
    return np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def test_edge_prefers_banner_over_empty_gold() -> None:
    banner_a = _banner_like(0)
    banner_b = _banner_like(2)  # same structure, different noise
    empty = _empty_hud(3)

    edge_same = patch_edge_similarity(banner_a, banner_b)
    edge_empty = patch_edge_similarity(banner_a, empty)
    hist_empty = patch_hist_similarity(banner_a, empty)

    assert edge_same > edge_empty
    # Critical: hist alone often thinks empty gold ≈ banner; combined must not.
    combined_empty = patch_similarity(banner_a, empty)
    combined_same = patch_similarity(banner_a, banner_b)
    assert combined_same > combined_empty
    assert combined_same >= 0.35
    # Even if hist is high, combined should stay below a strict teach threshold for empty.
    assert not (hist_empty > 0.45 and combined_empty >= 0.42)


def test_pos_require_edge_env_roundtrip(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_BANNER_POS_REQUIRE_EDGE", "1")
    monkeypatch.setenv("MLBB_BANNER_EDGE_MIN_SIM", "0.28")
    assert os.environ["MLBB_BANNER_POS_REQUIRE_EDGE"] == "1"
