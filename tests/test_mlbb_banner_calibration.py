"""Tests for MLBB banner screenshot calibration."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_banner_calibration_reasons import (  # noqa: E402
    BANNER_CALIB_REASONS,
    REASON_CODES,
    inline_keyboard_markup,
    labeled_keyboard_markup,
    reason_from_short,
)
from mlbb_banner_calibration_store import (  # noqa: E402
    apply_owner_label,
    check_id,
    labeled_ids,
    stats,
    upsert_check,
)


def test_reason_codes_and_short_tokens() -> None:
    assert len(BANNER_CALIB_REASONS) == 9
    assert REASON_CODES == {
        "no_banner",
        "not_kill",
        "wrong_hero",
        "enemy_kill",
        "not_enemy_kill",
        "not_gameplay",
        "own_kill_good",
        "savage_tier",
        "double_triple",
    }
    for code, short, _label in BANNER_CALIB_REASONS:
        assert reason_from_short(short) == code


def test_inline_keyboard_callback_length() -> None:
    cid = "abcdefghijk_346"
    markup = inline_keyboard_markup(cid)
    buttons = [b for row in markup["inline_keyboard"] for b in row]
    assert len(buttons) == 9
    for btn in buttons:
        assert len(btn["callback_data"]) <= 64
        assert btn["callback_data"].startswith(f"mlbb_bcal:{cid}:")


def test_labeled_keyboard_shows_reason() -> None:
    markup = labeled_keyboard_markup("enemy_kill")
    assert "противника" in markup["inline_keyboard"][0][0]["text"]


def test_apply_owner_label_saves_crop(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    shots = tmp_path / "shots"
    ref_root = tmp_path / "banners"
    vod = tmp_path / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"x" * 1000)
    shots.mkdir(parents=True)
    shot = shots / "abcdefghijk_120.jpg"
    shot.write_bytes(b"fakejpg")

    monkeypatch.setenv("MLBB_DATA_ROOT", str(data_root))
    monkeypatch.setenv("MLBB_BANNER_CALIB_SHOTS", str(shots))
    monkeypatch.setenv("MLBB_BANNER_REF_ROOT", str(ref_root))
    monkeypatch.setenv("MLBB_BANNER_CALIB_INDEX", str(data_root / "index.json"))
    monkeypatch.setenv("MLBB_BANNER_CALIB_LABELS", str(data_root / "labels.json"))
    monkeypatch.setenv("MLBB_BANNER_CALIB_SENT", str(data_root / "sent.json"))
    monkeypatch.setenv("MLBB_OWNER_LABELS_PATH", str(data_root / "owner.json"))

    import mlbb_banner_calibration_store as store

    monkeypatch.setattr(store, "_data_mlbb", lambda: data_root)
    monkeypatch.setattr(store, "_shots_root", lambda: shots)

    cid = check_id(vod, 120.0)
    upsert_check(
        {
            "check_id": cid,
            "vod": str(vod),
            "sec": 120.0,
            "screenshot": str(shot),
            "banner_tier": 5,
            "banner_label": "savage",
        }
    )

    fake_patch = np.zeros((48, 160, 3), dtype=np.uint8)
    with (
        patch("mlbb_banner_ref_ingest.crop_from_vod", return_value=ref_root / "vod_crops" / "savage" / f"{cid}.png"),
        patch("mlbb_banner_ref_ingest.extract_banner_crop", return_value=fake_patch),
        patch("mlbb_banner_ref_ingest.write_manifest", return_value={}),
        patch("mlbb_banner_ref_match.clear_banner_ref_cache"),
        patch("gameplay_gate._read_frame_at", return_value=fake_patch),
    ):
        (ref_root / "vod_crops" / "savage").mkdir(parents=True)
        (ref_root / "vod_crops" / "savage" / f"{cid}.png").write_bytes(b"png")
        ok, reason = apply_owner_label(cid, "savage_tier", by_chat="123")
    assert ok and reason == "savage_tier"
    assert labeled_ids()[cid] == "savage_tier"
    labels = json.loads((data_root / "labels.json").read_text(encoding="utf-8"))
    assert labels["labels"][0]["reason"] == "savage_tier"


def test_apply_owner_label_removes_negative_from_index(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    shots = tmp_path / "shots"
    ref_root = tmp_path / "banners"
    vod = tmp_path / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"x" * 1000)
    shots.mkdir(parents=True)
    shot = shots / "abcdefghijk_120.jpg"
    shot.write_bytes(b"fakejpg")

    monkeypatch.setenv("MLBB_DATA_ROOT", str(data_root))
    monkeypatch.setenv("MLBB_BANNER_CALIB_SHOTS", str(shots))
    monkeypatch.setenv("MLBB_BANNER_REF_ROOT", str(ref_root))
    monkeypatch.setenv("MLBB_BANNER_CALIB_INDEX", str(data_root / "index.json"))
    monkeypatch.setenv("MLBB_BANNER_CALIB_LABELS", str(data_root / "labels.json"))
    monkeypatch.setenv("MLBB_BANNER_CALIB_SENT", str(data_root / "sent.json"))

    import mlbb_banner_calibration_store as store

    monkeypatch.setattr(store, "_data_mlbb", lambda: data_root)
    monkeypatch.setattr(store, "_shots_root", lambda: shots)

    cid = check_id(vod, 120.0)
    upsert_check({"check_id": cid, "vod": str(vod), "sec": 120.0, "screenshot": str(shot)})

    fake_patch = np.zeros((48, 160, 3), dtype=np.uint8)
    with (
        patch("mlbb_banner_ref_ingest.write_manifest", return_value={}),
        patch("mlbb_banner_ref_match.clear_banner_ref_cache"),
        patch("gameplay_gate._read_frame_at", return_value=fake_patch),
        patch("mlbb_banner_ref_ingest.extract_banner_crop", return_value=fake_patch),
    ):
        ok, reason = apply_owner_label(cid, "no_banner", by_chat="123")
    assert ok and reason == "no_banner"
    assert store.load_index().get("checks") == []


def test_stats_target_remaining(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    monkeypatch.setenv("MLBB_DATA_ROOT", str(data_root))
    monkeypatch.setenv("MLBB_BANNER_CALIB_LABELS", str(data_root / "labels.json"))
    monkeypatch.setenv("MLBB_BANNER_CALIB_INDEX", str(data_root / "index.json"))
    monkeypatch.setenv("MLBB_BANNER_CALIB_SENT", str(data_root / "sent.json"))
    monkeypatch.setenv("MLBB_BANNER_CALIB_TARGET", "50")

    import mlbb_banner_calibration_store as store

    monkeypatch.setattr(store, "_data_mlbb", lambda: data_root)
    s = stats()
    assert s["target"] == 50
    assert s["remaining_to_target"] == 50
