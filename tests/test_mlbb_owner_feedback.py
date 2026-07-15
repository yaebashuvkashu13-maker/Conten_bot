"""Tests for the shared MLBB post-label feedback hook."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_owner_feedback import invalidate_vod_feedback, record_owner_feedback  # noqa: E402


def test_invalidate_vod_feedback_clears_stale_pool(tmp_path: Path) -> None:
    state_path = tmp_path / "vod_state.json"
    state_path.write_text(
        json.dumps(
            {
                "vods": [
                    {
                        "id": "abcdefghijk",
                        "path": "/data/yt_abcdefghijk.mp4",
                        "last_pool_peaks": [{"peak_sec": 100.0}],
                        "last_pool_at": 1,
                        "last_scan_blocked": True,
                        "exhausted": True,
                        "zero_send_sessions": 3,
                    }
                ]
            }
        )
    )

    assert invalidate_vod_feedback("abcdefghijk", state_path=state_path) == 1
    row = json.loads(state_path.read_text())["vods"][0]
    assert "last_pool_peaks" not in row
    assert "last_scan_blocked" not in row
    assert "zero_send_sessions" not in row
    assert row["exhausted"] is False
    assert row["feedback_epoch"] > 0


def test_record_owner_feedback_versions_manifest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MLBB_VOD_STATE_PATH", str(tmp_path / "missing_state.json"))
    monkeypatch.setenv(
        "MLBB_OWNER_FEEDBACK_MANIFEST",
        str(tmp_path / "owner_feedback_manifest.json"),
    )
    monkeypatch.setenv("MLBB_LEARNING_STATE", str(tmp_path / "learning_state.json"))
    monkeypatch.setenv("MLBB_LEARNING_FIRST", "1")
    monkeypatch.setenv("MLBB_SEND_ENABLED", "0")
    (tmp_path / "learning_state.json").write_text(
        json.dumps({"transition_passed": True, "daily_sends": {}}),
        encoding="utf-8",
    )

    first = record_owner_feedback(
        source="vod_segment",
        video_id="abcdefghijk",
        time_sec=100,
        label="bad",
        reason="boring",
        item_id="abcdefghijk_90",
    )
    second = record_owner_feedback(
        source="banner_calibration",
        video_id="/data/yt_abcdefghijk.mp4",
        time_sec=101,
        label="good",
        reason="own_kill_good",
        item_id="check-1",
    )

    manifest = json.loads((tmp_path / "owner_feedback_manifest.json").read_text())
    assert first["version"] == 1
    assert second["version"] == 2
    assert manifest["counts"]["vod_segment:bad"] == 1
    assert manifest["counts"]["banner_calibration:good"] == 1
    state = json.loads((tmp_path / "learning_state.json").read_text())
    assert state["transition_passed"] is False


def test_good_feedback_does_not_pause_sends(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MLBB_VOD_STATE_PATH", str(tmp_path / "missing_state.json"))
    monkeypatch.setenv(
        "MLBB_OWNER_FEEDBACK_MANIFEST",
        str(tmp_path / "owner_feedback_manifest.json"),
    )
    monkeypatch.setenv("MLBB_LEARNING_STATE", str(tmp_path / "learning_state.json"))
    monkeypatch.setenv("MLBB_LEARNING_FIRST", "1")
    monkeypatch.setenv("MLBB_SEND_ENABLED", "0")
    (tmp_path / "learning_state.json").write_text(
        json.dumps({"transition_passed": True, "daily_sends": {}}),
        encoding="utf-8",
    )

    record_owner_feedback(
        source="vod_segment",
        video_id="abcdefghijk",
        time_sec=100,
        label="good",
        reason="ok",
        item_id="abcdefghijk_90",
    )

    state = json.loads((tmp_path / "learning_state.json").read_text())
    assert state["transition_passed"] is True
