"""Owner label learning must block garbage sends."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_banner_calibration_positive_feed import positive_candidate_ok, verified_before_send  # noqa: E402
from mlbb_kill_banner import KillBannerHit  # noqa: E402


def test_segment_source_requires_ocr() -> None:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    hit = KillBannerHit(sec=10.0, tier=5, label="savage", text="segment_near", source="segment")
    with patch("mlbb_banner_ref_match.match_negative_banner_reference", return_value=None), patch(
        "mlbb_banner_ref_match.match_positive_owner_reference",
        return_value=None,
    ), patch("mlbb_kill_banner._ocr_banner_zones", return_value=""), patch(
        "mlbb_kill_banner.classify_banner_text",
        return_value=None,
    ):
        assert not positive_candidate_ok(hit, frame)


def test_verified_before_send_rejects_owner_neg(tmp_path: Path, monkeypatch) -> None:
    prof = tmp_path / "banner_calibration_profile.json"
    prof.write_text(json.dumps({"labeled": 90, "by_reason": {"no_banner": 60}}))
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MLBB_BANNER_OWNER_GATE", "1")
    monkeypatch.setenv("MLBB_BANNER_OWNER_GATE_MIN_LABELS", "20")

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    hit = KillBannerHit(sec=1.0, tier=3, label="triple", text="kill", source="ocr")
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"x" * 1000)
    with patch(
        "mlbb_banner_ref_match.match_negative_banner_reference",
        return_value=(0.5, "no_banner", "/fake.png"),
    ), patch("mlbb_banner_ref_match.match_positive_owner_reference", return_value=None):
        ok, reason = verified_before_send(vod, hit, frame)
    assert not ok
    assert "candidate_filter" in reason or "owner_neg" in reason
