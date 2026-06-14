"""MLBB Shorts reject — wrong-game must never reach owner again."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_reject_candidate_removes_from_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_root = tmp_path / "data"
    shorts_root = tmp_path / "shorts"
    shorts_root.mkdir()
    data_root.mkdir()
    vid = "FPcivM1RIUc"
    (shorts_root / f"yt_{vid}.mp4").write_bytes(b"x")

    monkeypatch.setenv("MLBB_DATA_ROOT", str(data_root))
    monkeypatch.setenv("MLBB_SHORTS_ROOT", str(shorts_root))
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(tmp_path / "exemplars"))

    import mlbb_calibration_store as store

    monkeypatch.setattr(store, "DATA_MLBB", data_root)
    monkeypatch.setattr(store, "SHORTS_ROOT", shorts_root)
    monkeypatch.setattr(store, "INDEX_PATH", data_root / "youtube_shorts_index.json")
    monkeypatch.setattr(store, "LABELS_PATH", data_root / "calibration_labels.json")
    monkeypatch.setattr(store, "FEED_SENT_PATH", data_root / "calibration_feed_sent.json")
    monkeypatch.setattr(store, "EXEMPLAR_ROOT", tmp_path / "exemplars")
    monkeypatch.setattr(
        store,
        "copy_exemplar",
        lambda src, label, video_id: tmp_path / "exemplars" / f"{label}_{video_id}.mp4",
    )

    store.save_index(
        {
            "candidates": [
                {"video_id": vid, "path": str(shorts_root / f"yt_{vid}.mp4"), "score": 0.0}
            ]
        }
    )
    store.save_labels({"good": [], "bad": [], "feedback": []})

    store.reject_candidate(vid, reason="not_mlbb:heuristic", path=shorts_root / f"yt_{vid}.mp4")

    assert store.load_index()["candidates"] == []
    labels = store.load_labels()
    assert any(r.get("video_id") == vid for r in labels["bad"])
    assert vid in store.load_feed_sent()["ids"]
