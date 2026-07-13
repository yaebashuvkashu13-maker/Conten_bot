"""Tests for banner calibration apply + owner gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_banner_calibration_gate import check_banner_frame, gate_enabled  # noqa: E402


def test_gate_disabled_without_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MLBB_BANNER_OWNER_GATE", "1")
    assert not gate_enabled()


def test_gate_rejects_negative_match(tmp_path: Path, monkeypatch) -> None:
    prof = tmp_path / "banner_calibration_profile.json"
    prof.write_text(json.dumps({"labeled": 30, "by_reason": {"no_banner": 20}}))
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MLBB_BANNER_OWNER_GATE", "1")
    monkeypatch.setenv("MLBB_BANNER_OWNER_GATE_MIN_LABELS", "20")

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    with patch(
        "mlbb_banner_ref_match.match_negative_banner_reference",
        return_value=(0.55, "no_banner", "/fake.png"),
    ), patch("mlbb_banner_ref_match.match_positive_owner_reference", return_value=None):
        decision, reason = check_banner_frame(frame, tier=3)
    assert decision == "reject"
    assert "no_banner" in reason


def test_stronger_positive_evidence_overrides_negative_match(
    tmp_path: Path, monkeypatch
) -> None:
    prof = tmp_path / "banner_calibration_profile.json"
    prof.write_text(json.dumps({"labeled": 110}))
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MLBB_BANNER_OWNER_GATE", "1")
    monkeypatch.setenv("MLBB_BANNER_OWNER_EVIDENCE_MARGIN", "0.05")
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    with patch(
        "mlbb_banner_ref_match.match_positive_owner_reference",
        return_value=(0.912, "not_enemy_kill", "/positive.png"),
    ), patch(
        "mlbb_banner_ref_match.match_negative_banner_reference",
        return_value=(0.803, "not_kill", "/negative.png"),
    ):
        decision, reason = check_banner_frame(frame, tier=3)
    assert decision == "pass"
    assert "not_enemy_kill" in reason


def test_stronger_negative_evidence_still_rejects(tmp_path: Path, monkeypatch) -> None:
    prof = tmp_path / "banner_calibration_profile.json"
    prof.write_text(json.dumps({"labeled": 110}))
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MLBB_BANNER_OWNER_GATE", "1")
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    with patch(
        "mlbb_banner_ref_match.match_positive_owner_reference",
        return_value=(0.60, "own_kill_good", "/positive.png"),
    ), patch(
        "mlbb_banner_ref_match.match_negative_banner_reference",
        return_value=(0.82, "enemy_kill", "/negative.png"),
    ):
        decision, reason = check_banner_frame(frame, tier=3)
    assert decision == "reject"
    assert "enemy_kill" in reason


def test_strict_send_rejects_neutral_without_visual_proof(tmp_path: Path, monkeypatch) -> None:
    prof = tmp_path / "banner_calibration_profile.json"
    prof.write_text(json.dumps({"labeled": 75, "by_reason": {"no_banner": 75}}))
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MLBB_BANNER_OWNER_GATE", "1")
    monkeypatch.setenv("MLBB_BANNER_SEND_STRICT", "1")

    from mlbb_banner_calibration_gate import check_banner_frame_passes

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    with patch(
        "mlbb_banner_ref_match.match_negative_banner_reference",
        return_value=None,
    ), patch(
        "mlbb_banner_ref_match.match_positive_owner_reference",
        return_value=None,
    ), patch(
        "mlbb_banner_ref_match.match_banner_reference",
        return_value=None,
    ):
        ok, reason = check_banner_frame_passes(frame, tier=2)
    assert not ok
    assert "no_banner_visual_proof" in reason


def test_write_profile_from_stats(tmp_path: Path, monkeypatch) -> None:
    labels = tmp_path / "labels.json"
    labels.write_text(
        json.dumps(
            {
                "labels": [
                    {"check_id": "a_1", "reason": "no_banner", "vod": "/x.mp4", "sec": 1},
                    {"check_id": "a_2", "reason": "own_kill_good", "vod": "/x.mp4", "sec": 2},
                ]
            }
        )
    )
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"checks": []}))
    sent = tmp_path / "sent.json"
    sent.write_text(json.dumps({"sent_ids": []}))
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MLBB_BANNER_CALIB_LABELS", str(labels))
    monkeypatch.setenv("MLBB_BANNER_CALIB_INDEX", str(index))
    monkeypatch.setenv("MLBB_BANNER_CALIB_SENT", str(sent))
    monkeypatch.setenv("MLBB_BANNER_REF_ROOT", str(tmp_path / "banners"))

    import mlbb_banner_calibration_apply as apply

    monkeypatch.setattr(apply, "_banner_ref_root", lambda: tmp_path / "banners")
    (tmp_path / "banners" / "owner_cal" / "negative" / "no_banner").mkdir(parents=True)
    (tmp_path / "banners" / "owner_cal" / "positive" / "own_kill_good").mkdir(parents=True)
    (tmp_path / "banners" / "owner_cal" / "negative" / "no_banner" / "a_1.png").write_bytes(b"x")
    (tmp_path / "banners" / "owner_cal" / "positive" / "own_kill_good" / "a_2.png").write_bytes(b"x")

    profile = apply.write_profile()
    assert profile["labeled"] == 2
    assert profile["gate_active"] is False
    assert "MLBB_BANNER_NEG_REF_MIN_SIM" in profile["thresholds"]
