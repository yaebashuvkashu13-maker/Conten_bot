"""Tests for unified MLBB owner learning (Shorts + VOD)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_owner_learning import (  # noqa: E402
    append_owner_time_label,
    backfill_shorts_to_owner_labels,
    build_training_manifest,
    load_owner_labels_json,
    load_unified_training_samples,
    owner_kill_anchor_secs,
    sync_shorts_label_to_owner_json,
)


def test_sync_shorts_label_writes_owner_json(tmp_path: Path, monkeypatch) -> None:
    owner = tmp_path / "owner.json"
    shorts = tmp_path / "shorts_labels.json"
    shorts.write_text(json.dumps({"good": [], "bad": [], "feedback": []}))
    monkeypatch.setenv("MLBB_OWNER_LABELS_PATH", str(owner))
    monkeypatch.setenv("MLBB_CALIBRATION_LABELS", str(shorts))

    assert sync_shorts_label_to_owner_json("abcdefghijk", is_good=True, reason="")
    data = json.loads(owner.read_text())
    rows = data["videos"]["abcdefghijk"]
    assert rows[0]["label"] == "good"
    assert rows[0]["source"] == "youtube_shorts"
    assert rows[0]["scope"] == "full_clip"


def test_backfill_shorts_to_owner_labels(tmp_path: Path, monkeypatch) -> None:
    owner = tmp_path / "owner.json"
    shorts = tmp_path / "shorts_labels.json"
    shorts.write_text(
        json.dumps(
            {
                "good": [{"video_id": "aaa11111111", "path": "/x/yt_aaa11111111.mp4"}],
                "bad": [{"video_id": "bbb22222222", "path": "/x/yt_bbb22222222.mp4", "reason": "promo"}],
            }
        )
    )
    monkeypatch.setenv("MLBB_OWNER_LABELS_PATH", str(owner))
    monkeypatch.setenv("MLBB_CALIBRATION_LABELS", str(shorts))

    n = backfill_shorts_to_owner_labels()
    assert n == 2
    data = json.loads(owner.read_text())
    assert "aaa11111111" in data["videos"]
    assert "bbb22222222" in data["videos"]
    assert data["videos"]["bbb22222222"][0]["note"] == "promo"


def test_unified_training_dedupes_exemplar_and_shorts(tmp_path: Path, monkeypatch) -> None:
    ex_root = tmp_path / "exemplars" / "mobile_legends"
    (ex_root / "good").mkdir(parents=True)
    clip = ex_root / "good" / "cal_abcdefghijk.mp4"
    clip.write_bytes(b"x" * 5000)
    shorts_path = tmp_path / "yt_abcdefghijk.mp4"
    shorts_path.write_bytes(b"x" * 5000)

    shorts_labels = tmp_path / "calibration_labels.json"
    shorts_labels.write_text(
        json.dumps(
            {
                "good": [
                    {
                        "video_id": "abcdefghijk",
                        "path": str(shorts_path),
                    }
                ],
                "bad": [],
            }
        )
    )
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(tmp_path / "exemplars"))
    monkeypatch.setenv("MLBB_CALIBRATION_LABELS", str(shorts_labels))
    monkeypatch.setenv("MLBB_VOD_SEGMENT_LABELS", str(tmp_path / "vod_segment_labels.json"))
    monkeypatch.setenv("MLBB_OWNER_LABELS_PATH", str(tmp_path / "owner.json"))

    samples = load_unified_training_samples("mobile_legends")
    paths = {p.name for p, _s, _l in samples}
    assert "cal_abcdefghijk.mp4" in paths
    assert "yt_abcdefghijk.mp4" not in paths


def test_owner_kill_anchor_secs_filters_notes(tmp_path: Path, monkeypatch) -> None:
    owner = tmp_path / "owner.json"
    monkeypatch.setenv("MLBB_OWNER_LABELS_PATH", str(owner))
    append_owner_time_label("opuealwWYA0", 49.0, "good", note="double_kill", source="owner")
    append_owner_time_label("opuealwWYA0", 10.0, "good", note="bad spawn", source="owner")
    secs = owner_kill_anchor_secs("opuealwWYA0")
    assert secs == [49.0]


def test_append_owner_time_label_dedupes(tmp_path: Path, monkeypatch) -> None:
    owner = tmp_path / "owner.json"
    monkeypatch.setenv("MLBB_OWNER_LABELS_PATH", str(owner))

    assert append_owner_time_label("vid12345678", 100.0, "good", source="vod_segment")
    assert not append_owner_time_label("vid12345678", 100.0, "good", source="vod_segment")
    assert len(load_owner_labels_json()["videos"]["vid12345678"]) == 1


def test_banner_scope_is_not_clip_ranker_training_data(tmp_path: Path, monkeypatch) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    vod = inbox / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"x" * 5000)
    owner = tmp_path / "owner.json"
    monkeypatch.setenv("HIGHLIGHT_INBOX", str(inbox))
    monkeypatch.setenv("MLBB_OWNER_LABELS_PATH", str(owner))
    monkeypatch.setenv("MLBB_CALIBRATION_LABELS", str(tmp_path / "shorts.json"))
    monkeypatch.setenv("MLBB_VOD_SEGMENT_LABELS", str(tmp_path / "vseg.json"))
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(tmp_path / "exemplars"))

    append_owner_time_label(
        "abcdefghijk",
        50,
        "good",
        source="banner_calibration",
        scope="banner",
    )
    append_owner_time_label(
        "abcdefghijk",
        100,
        "bad",
        source="preview_bad",
        scope="segment",
    )

    samples = load_unified_training_samples("mobile_legends")
    assert len(samples) == 1
    assert samples[0][2] == 0


def test_training_manifest_keeps_banner_task_separate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("mlbb_owner_learning.DATA_MLBB", tmp_path)
    monkeypatch.setenv("MLBB_OWNER_LABELS_PATH", str(tmp_path / "owner.json"))
    monkeypatch.setenv("MLBB_CALIBRATION_LABELS", str(tmp_path / "shorts.json"))
    monkeypatch.setenv("MLBB_VOD_SEGMENT_LABELS", str(tmp_path / "vseg.json"))
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(tmp_path / "exemplars"))
    (tmp_path / "banner_calibration_labels.json").write_text(
        json.dumps({"labels": [{"reason": "no_banner"}, {"reason": "own_kill_good"}]})
    )

    manifest = build_training_manifest(write=False)
    assert manifest["clip_ranker"]["banner_scope_excluded"] is True
    assert manifest["banner_event"]["samples"] == 2
    assert manifest["banner_event"]["by_reason"]["no_banner"] == 1
