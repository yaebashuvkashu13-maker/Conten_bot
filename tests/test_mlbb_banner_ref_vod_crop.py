"""Ref-match must not treat generic HUD gold as vod_crop savage."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_banner_calibration_positive_feed import positive_candidate_ok  # noqa: E402
from mlbb_banner_ref_match import (  # noqa: E402
    clear_banner_ref_cache,
    match_banner_reference,
)
from mlbb_kill_banner import KillBannerHit  # noqa: E402


def _write_manifest(tmp_path: Path, refs: list[dict]) -> None:
    root = tmp_path / "banners"
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps({"refs": refs}),
        encoding="utf-8",
    )


def test_vod_crop_excluded_from_generic_match_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_BANNER_REF_ROOT", str(tmp_path / "banners"))
    clear_banner_ref_cache()

    wiki = tmp_path / "banners" / "wiki" / "classic.png"
    wiki.parent.mkdir(parents=True)
    vod = tmp_path / "banners" / "vod_crops" / "savage" / "bad_320s.png"
    vod.parent.mkdir(parents=True)
    patch_img = np.zeros((48, 160, 3), dtype=np.uint8)
    patch_img[:, :, 1] = 180
    patch_img[:, :, 2] = 220
    cv2.imwrite(str(wiki), patch_img)
    cv2.imwrite(str(vod), patch_img)

    _write_manifest(
        tmp_path,
        [
            {"path": "wiki/classic.png", "name": "classic", "source": "wiki", "tier_hint": "unknown"},
            {"path": "vod_crops/savage/bad_320s.png", "name": "bad_320s", "source": "vod_crop", "tier_hint": "savage"},
        ],
    )
    clear_banner_ref_cache()

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    h, w = frame.shape[:2]
    y0, y1 = int(h * 0.02), int(h * 0.30)
    x0, x1 = int(w * 0.15), int(w * 0.85)
    frame[y0:y1, x0:x1] = cv2.resize(patch_img, (x1 - x0, y1 - y0))

    row = match_banner_reference(frame)
    assert row is not None
    _score, _name, source, tier = row
    assert source == "wiki"
    assert tier <= 2


def test_positive_candidate_rejects_ref_only(tmp_path: Path, monkeypatch) -> None:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    hit = KillBannerHit(
        sec=120.0,
        tier=5,
        label="savage",
        text="ref=bad_320s sim=0.930 src=vod_crop",
        source="ref",
    )
    with patch("mlbb_banner_ref_match.match_negative_banner_reference", return_value=None), patch(
        "mlbb_banner_ref_match.match_positive_owner_reference",
        return_value=None,
    ), patch("mlbb_kill_banner._ocr_banner_zones", return_value=""), patch(
        "mlbb_kill_banner.classify_banner_text",
        return_value=None,
    ):
        assert not positive_candidate_ok(hit, frame)


def test_purge_vod_crops_on_negative_label(tmp_path: Path, monkeypatch) -> None:
    ref_root = tmp_path / "banners"
    crop = ref_root / "vod_crops" / "savage" / "abcdefghijk_120.png"
    crop.parent.mkdir(parents=True)
    crop.write_bytes(b"png")
    pos = ref_root / "owner_cal" / "positive" / "savage_tier" / "abcdefghijk_120.png"
    pos.parent.mkdir(parents=True)
    pos.write_bytes(b"png")

    monkeypatch.setenv("MLBB_BANNER_REF_ROOT", str(ref_root))
    from mlbb_banner_calibration_store import purge_positive_crops_for_check, purge_vod_crops_for_row

    row = {"check_id": "abcdefghijk_120", "vod": str(tmp_path / "yt_abcdefghijk.mp4"), "sec": 120.0}
    removed = purge_vod_crops_for_row(row)
    assert any("abcdefghijk_120" in p for p in removed)
    removed_pos = purge_positive_crops_for_check("abcdefghijk_120")
    assert removed_pos
